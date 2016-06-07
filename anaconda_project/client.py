# -*- coding: utf-8 -*-
# ----------------------------------------------------------------------------
# Copyright © 2016, Continuum Analytics, Inc. All rights reserved.
#
# The full license is in the file LICENSE.txt, distributed with this software.
# ----------------------------------------------------------------------------
"""Talking to the Anaconda server."""
from __future__ import absolute_import, print_function

import logging
import os
import tarfile
import zipfile

import requests
import binstar_client.utils as binstar_utils
import binstar_client.requests_ext as binstar_requests_ext
from binstar_client.errors import BinstarError, Unauthorized

from anaconda_project.internal.simple_status import SimpleStatus


class _Client(object):
    def __init__(self, site=None, username=None, token=None, log_level=None):
        assert hasattr(binstar_utils, 'get_server_api'), "Please upgrade anaconda-client"
        if log_level is None:
            log_level = logging.INFO
        self._api = binstar_utils.get_server_api(site=site, token=token, log_level=log_level)
        self._user_info = None
        self._force_username = username

    def _username(self):
        """Get username to upload to; raise Unauthorized if we aren't logged in."""
        if self._user_info is None:
            self._user_info = self._api.user(login=self._force_username)
        assert self._user_info is not None
        username = self._user_info.get('login', None)
        if username is None:
            raise Unauthorized()
        else:
            return username

    def _check_response(self, res, allowed=(200, 201)):
        # using a little private API here.
        self._api._check_response(res, allowed=list(allowed))

    # HACK ALERT: using this is a workaround for
    # https://github.com/Anaconda-Platform/anaconda-server/issues/2229
    def _exists(self, project_name):
        url = "{}/apps/{}/projects/{}".format(self._api.domain, self._username(), project_name)
        res = self._api.session.get(url)
        return res.status_code == 200

    def create(self, project_info):
        url = "{}/apps/{}/projects".format(self._api.domain, self._username())
        json = {'name': project_info['name'],
                'access': 'public',
                'profile': {'description': project_info['description']}}
        data, headers = binstar_utils.jencode(json)
        res = self._api.session.post(url, data=data, headers=headers)
        self._check_response(res)
        return res

    def _file_count(self, bundle_filename):
        for suffix in (".tar", ".tar.gz", ".tar.bz2"):
            if bundle_filename.lower().endswith(suffix):
                with tarfile.open(bundle_filename, 'r') as tf:
                    return len(tf.getnames())
        if bundle_filename.lower().endswith(".zip"):
            with zipfile.ZipFile(bundle_filename, 'r') as zf:
                return len(zf.namelist())
        assert False, ("unsupported bundle filename %s" % bundle_filename)  # pragma: no cover (should not be reached)

    def stage(self, project_info, bundle_filename):
        url = "{}/apps/{}/projects/{}/stage".format(self._api.domain, self._username(), project_info['name'])
        config = project_info.copy()
        config['size'] = os.path.getsize(bundle_filename)
        file_count = self._file_count(bundle_filename)
        if file_count is not None:
            config['num_of_files'] = file_count
        json = {'basename': ("%s.tar" % project_info['name']), 'configuration': config}
        data, headers = binstar_utils.jencode(json)
        res = self._api.session.post(url, data=data, headers=headers)
        self._check_response(res)
        return res

    def commit(self, project_name, dist_id):

        url = "{}/apps/{}/projects/{}/commit/{}".format(self._api.domain, self._username(), project_name, dist_id)
        data, headers = binstar_utils.jencode({})
        res = self._api.session.post(url, data=data, headers=headers)
        self._check_response(res)
        return res

    def _put_on_s3(self, bundle_filename, url, s3data):
        with open(bundle_filename, 'rb') as f:
            _hexmd5, b64md5, size = binstar_utils.compute_hash(f, size=os.path.getsize(bundle_filename))

        s3data = s3data.copy()  # don't modify our parameters
        s3data['Content-Length'] = size
        s3data['Content-MD5'] = b64md5

        with open(bundle_filename, 'rb') as bundle_file_object:
            data_stream, headers = binstar_requests_ext.stream_multipart(
                s3data,
                files={'file': (os.path.basename(bundle_filename), bundle_file_object)})

            res = requests.post(url,
                                data=data_stream,
                                verify=self._api.session.verify,
                                timeout=10 * 60 * 60,
                                headers=headers)
            self._check_response(res)
        return res

    def upload(self, project_info, bundle_filename):
        """Upload bundle_filename created from project, throwing BinstarError."""
        if not self._exists(project_info['name']):
            res = self.create(project_info=project_info)
            assert res.status_code in (200, 201)

        res = self.stage(project_info=project_info, bundle_filename=bundle_filename)
        assert res.status_code in (200, 201)

        stage_info = res.json()

        assert 'post_url' in stage_info
        assert 'form_data' in stage_info
        assert 'dist_id' in stage_info

        res = self._put_on_s3(bundle_filename, url=stage_info['post_url'], s3data=stage_info['form_data'])
        assert res.status_code in (200, 201)

        res = self.commit(project_info['name'], stage_info['dist_id'])
        assert res.status_code in (200, 201)

        return res.json()


class _UploadedStatus(SimpleStatus):
    def __init__(self, json):
        self.url = json.get('url', None)
        logs = []
        if self.url is not None:
            logs.append("Project is at %s" % self.url)
        super(_UploadedStatus, self).__init__(success=True, description="Upload successful.", logs=logs)


# This function is supposed to encapsulate the binstar API (don't
# require any other files to import binstar_client).
def _upload(project, bundle_filename, site=None, username=None, token=None, log_level=None):
    assert not project.problems

    client = _Client(site=site, username=username, token=token, log_level=log_level)
    try:
        json = client.upload(project.publication_info(), bundle_filename)
        return _UploadedStatus(json)
    except Unauthorized as e:
        return SimpleStatus(success=False,
                            description='Please log in with the "anaconda login" command.',
                            errors=["Not logged in."])
    except BinstarError as e:
        return SimpleStatus(success=False, description="Upload failed.", errors=[str(e)])
