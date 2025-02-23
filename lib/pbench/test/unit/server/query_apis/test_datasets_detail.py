from http import HTTPStatus

import pytest

from pbench.server.api.resources.query_apis.datasets_detail import DatasetsDetail
from pbench.test.unit.server.query_apis.commons import Commons


class TestDatasetsDetail(Commons):
    """
    Unit testing for resources/DatasetsDetail class.

    In a web service context, we access class functions mostly via the
    Flask test client rather than trying to directly invoke the class
    constructor and `post` service.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, client):
        super()._setup(
            cls_obj=DatasetsDetail(client.config, client.logger),
            pbench_endpoint="/datasets/detail/fio",
            elastic_endpoint="/_search?ignore_unavailable=true",
            payload={
                "user": "drb",
                "access": "private",
                "start": "2020-08",
                "end": "2020-10",
            },
            empty_es_response_payload=self.EMPTY_ES_RESPONSE_PAYLOAD,
        )

    @pytest.mark.parametrize(
        "user",
        ("drb", "badwolf", "no_user"),
    )
    def test_query(
        self, client, server_config, query_api, find_template, build_auth_header, user
    ):
        """
        Check the construction of Elasticsearch query URI and filtering of the response body.
        The test will run once with each parameter supplied from the local parameterization,
        and, for each of those, three times with different values of the build_auth_header fixture.
        """
        dataset_name = "fio_rhel8_kvm_perf43_preallocfull_nvme_run4_iothread_isolcpus_2020.04.29T12.49.13"
        payload = {
            "user": user,
            "access": "private",
            "start": "2020-08",
            "end": "2020-10",
        }

        # We expect "no_user" to succeed, looking for only public data. We
        # normally remove the user parameter; for this case, we'll look for
        # public data owned by a known user; which should succeed.
        if user == "no_user":
            del payload["user"]
            payload["access"] = "public"

        response_payload = {
            "took": 112,
            "timed_out": False,
            "_shards": {"total": 5, "successful": 5, "skipped": 0, "failed": 0},
            "hits": {
                "total": {"value": 1, "relation": "eq"},
                "max_score": None,
                "hits": [
                    {
                        "_index": "drb.v6.run-data.2020-04",
                        "_type": "_doc",
                        "_id": "12fb1e952fd826727810868c9327254f",
                        "_score": None,
                        "_source": {
                            "@timestamp": "2020-04-29T12:49:13.560620",
                            "@metadata": {
                                "file-date": "2020-11-20T21:01:54.532281",
                                "file-name": "/pbench/archive/fs-version-001/dhcp31-187.example.com/fio_rhel8_kvm_perf43_preallocfull_nvme_run4_iothread_isolcpus_2020.04.29T12.49.13.tar.xz",
                                "file-size": 216319392,
                                "md5": "12fb1e952fd826727810868c9327254f",
                                "toc-prefix": "fio_rhel8_kvm_perf43_preallocfull_nvme_run4_iothread_isolcpus_2020.04.29T12.49.13",
                                "pbench-agent-version": "0.68-1gf4c94b4d",
                                "controller_dir": "dhcp31-187.example.com",
                                "tar-ball-creation-timestamp": "2020-04-29T15:16:51.880540",
                                "raw_size": 292124533,
                            },
                            "@generated-by": "3319a130c156f978fa6dc809012b5ba0",
                            "authorization": {"user": "unknown", "access": "private"},
                            "run": {
                                "controller": "dhcp31-187.example.com",
                                "name": "drb",
                                "script": "fio",
                                "config": "rhel8_kvm_perf43_preallocfull_nvme_run4_iothread_isolcpus",
                                "date": "2020-04-29T12:48:33",
                                "iterations": "0__bs=4k_iodepth=1_iodepth_batch_complete_max=1, 1__bs=32k_iodepth=1_iodepth_batch_complete_max=1, 2__bs=256k_iodepth=1_iodepth_batch_complete_max=1, 3__bs=4k_iodepth=8_iodepth_batch_complete_max=8, 4__bs=32k_iodepth=8_iodepth_batch_complete_max=8, 5__bs=256k_iodepth=8_iodepth_batch_complete_max=8, 6__bs=4k_iodepth=16_iodepth_batch_complete_max=16, 7__bs=32k_iodepth=16_iodepth_batch_complete_max=16, 8__bs=256k_iodepth=16_iodepth_batch_complete_max=16",
                                "toolsgroup": "default",
                                "start": "2020-04-29T12:49:13.560620",
                                "end": "2020-04-29T13:30:04.918704",
                                "id": "random_md5_string1",
                            },
                            "host_tools_info": [
                                {
                                    "hostname": "dhcp31-187",
                                    "tools": {
                                        "iostat": "--interval=3",
                                        "mpstat": "--interval=3",
                                        "perf": "--record-opts='record -a --freq=100'",
                                        "pidstat": "--interval=30",
                                        "proc-interrupts": "--interval=3",
                                        "proc-vmstat": "--interval=3",
                                        "sar": "--interval=3",
                                        "turbostat": "--interval=3",
                                    },
                                }
                            ],
                        },
                        "sort": ["drb.v6.run-data.2020-04"],
                    }
                ],
            },
        }

        index = self.build_index(
            server_config, self.date_range(self.payload["start"], self.payload["end"])
        )

        expected_status = HTTPStatus.OK

        expected_status = self.get_expected_status(
            payload, build_auth_header["header_param"]
        )
        response = query_api(
            f"/datasets/detail/{dataset_name}",
            self.elastic_endpoint,
            payload,
            index,
            expected_status,
            headers=build_auth_header["header"],
            json=response_payload,
        )
        assert response.status_code == expected_status
        if response.status_code == HTTPStatus.OK:
            res_json = response.json

            expected = {
                "hostTools": [
                    {
                        "hostname": "dhcp31-187",
                        "tools": {
                            "iostat": "--interval=3",
                            "mpstat": "--interval=3",
                            "perf": "--record-opts='record -a --freq=100'",
                            "pidstat": "--interval=30",
                            "proc-interrupts": "--interval=3",
                            "proc-vmstat": "--interval=3",
                            "sar": "--interval=3",
                            "turbostat": "--interval=3",
                        },
                    }
                ],
                "runMetadata": {
                    "config": "rhel8_kvm_perf43_preallocfull_nvme_run4_iothread_isolcpus",
                    "controller": "dhcp31-187.example.com",
                    "controller_dir": "dhcp31-187.example.com",
                    "date": "2020-04-29T12:48:33",
                    "end": "2020-04-29T13:30:04.918704",
                    "file-date": "2020-11-20T21:01:54.532281",
                    "file-name": "/pbench/archive/fs-version-001/dhcp31-187.example.com/fio_rhel8_kvm_perf43_preallocfull_nvme_run4_iothread_isolcpus_2020.04.29T12.49.13.tar.xz",
                    "file-size": 216319392,
                    "id": "random_md5_string1",
                    "iterations": "0__bs=4k_iodepth=1_iodepth_batch_complete_max=1, 1__bs=32k_iodepth=1_iodepth_batch_complete_max=1, 2__bs=256k_iodepth=1_iodepth_batch_complete_max=1, 3__bs=4k_iodepth=8_iodepth_batch_complete_max=8, 4__bs=32k_iodepth=8_iodepth_batch_complete_max=8, 5__bs=256k_iodepth=8_iodepth_batch_complete_max=8, 6__bs=4k_iodepth=16_iodepth_batch_complete_max=16, 7__bs=32k_iodepth=16_iodepth_batch_complete_max=16, 8__bs=256k_iodepth=16_iodepth_batch_complete_max=16",
                    "md5": "12fb1e952fd826727810868c9327254f",
                    "name": "drb",
                    "pbench-agent-version": "0.68-1gf4c94b4d",
                    "raw_size": 292124533,
                    "script": "fio",
                    "start": "2020-04-29T12:49:13.560620",
                    "tar-ball-creation-timestamp": "2020-04-29T15:16:51.880540",
                    "toc-prefix": "fio_rhel8_kvm_perf43_preallocfull_nvme_run4_iothread_isolcpus_2020.04.29T12.49.13",
                    "toolsgroup": "default",
                },
            }
            assert expected == res_json

    def test_metadata(
        self,
        client,
        server_config,
        query_api,
        find_template,
        provide_metadata,
        pbench_token,
    ):
        """
        This is nearly a repeat of the basic `test_query`; while that focuses
        on validating the transformation of Elasticsearch data, this tries to
        focus on the PostgreSQL dataset metadata... but necessarily has to
        borrow much of the setup.
        """
        dataset_name = "drb"
        payload = {
            "user": "drb",
            "start": "2020-08",
            "end": "2020-10",
            "metadata": ["global.seen", "server.deletion"],
        }

        response_payload = {
            "took": 112,
            "timed_out": False,
            "_shards": {"total": 5, "successful": 5, "skipped": 0, "failed": 0},
            "hits": {
                "total": {"value": 1, "relation": "eq"},
                "max_score": None,
                "hits": [
                    {
                        "_index": "drb.v6.run-data.2020-04",
                        "_type": "_doc",
                        "_id": "12fb1e952fd826727810868c9327254f",
                        "_score": None,
                        "_source": {
                            "@timestamp": "2020-04-29T12:49:13.560620",
                            "@metadata": {
                                "file-date": "2020-11-20T21:01:54.532281",
                                "file-name": "/pbench/archive/fs-version-001/dhcp31-187.example.com/fio_rhel8_kvm_perf43_preallocfull_nvme_run4_iothread_isolcpus_2020.04.29T12.49.13.tar.xz",
                                "file-size": 216319392,
                                "md5": "12fb1e952fd826727810868c9327254f",
                                "toc-prefix": "fio_rhel8_kvm_perf43_preallocfull_nvme_run4_iothread_isolcpus_2020.04.29T12.49.13",
                                "pbench-agent-version": "0.68-1gf4c94b4d",
                                "controller_dir": "dhcp31-187.example.com",
                                "tar-ball-creation-timestamp": "2020-04-29T15:16:51.880540",
                                "raw_size": 292124533,
                            },
                            "@generated-by": "3319a130c156f978fa6dc809012b5ba0",
                            "authorization": {"user": "unknown", "access": "private"},
                            "run": {
                                "controller": "node",
                                "name": "drb",
                                "script": "fio",
                                "config": "rhel8_kvm_perf43_preallocfull_nvme_run4_iothread_isolcpus",
                                "date": "2020-04-29T12:48:33",
                                "iterations": "0__bs=4k_iodepth=1_iodepth_batch_complete_max=1, 1__bs=32k_iodepth=1_iodepth_batch_complete_max=1, 2__bs=256k_iodepth=1_iodepth_batch_complete_max=1, 3__bs=4k_iodepth=8_iodepth_batch_complete_max=8, 4__bs=32k_iodepth=8_iodepth_batch_complete_max=8, 5__bs=256k_iodepth=8_iodepth_batch_complete_max=8, 6__bs=4k_iodepth=16_iodepth_batch_complete_max=16, 7__bs=32k_iodepth=16_iodepth_batch_complete_max=16, 8__bs=256k_iodepth=16_iodepth_batch_complete_max=16",
                                "toolsgroup": "default",
                                "start": "2020-04-29T12:49:13.560620",
                                "end": "2020-04-29T13:30:04.918704",
                                "id": "random_md5_string1",
                            },
                            "host_tools_info": [
                                {
                                    "hostname": "dhcp31-187",
                                    "tools": {
                                        "iostat": "--interval=3",
                                        "mpstat": "--interval=3",
                                        "perf": "--record-opts='record -a --freq=100'",
                                        "pidstat": "--interval=30",
                                        "proc-interrupts": "--interval=3",
                                        "proc-vmstat": "--interval=3",
                                        "sar": "--interval=3",
                                        "turbostat": "--interval=3",
                                    },
                                }
                            ],
                        },
                        "sort": ["drb.v6.run-data.2020-04"],
                    }
                ],
            },
        }

        index = self.build_index(
            server_config, self.date_range(self.payload["start"], self.payload["end"])
        )

        response = query_api(
            f"/datasets/detail/{dataset_name}",
            self.elastic_endpoint,
            payload,
            index,
            HTTPStatus.OK,
            headers={"authorization": f"Bearer {pbench_token}"},
            json=response_payload,
        )
        assert response.status_code == HTTPStatus.OK
        res_json = response.json

        # NOTE: we asked for "seen" and "deleted" metadata, but the "deleted"
        # key wasn't created, so we verify that it's reported as None.
        expected = {
            "hostTools": [
                {
                    "hostname": "dhcp31-187",
                    "tools": {
                        "iostat": "--interval=3",
                        "mpstat": "--interval=3",
                        "perf": "--record-opts='record -a --freq=100'",
                        "pidstat": "--interval=30",
                        "proc-interrupts": "--interval=3",
                        "proc-vmstat": "--interval=3",
                        "sar": "--interval=3",
                        "turbostat": "--interval=3",
                    },
                }
            ],
            "runMetadata": {
                "config": "rhel8_kvm_perf43_preallocfull_nvme_run4_iothread_isolcpus",
                "controller": "node",
                "controller_dir": "dhcp31-187.example.com",
                "date": "2020-04-29T12:48:33",
                "end": "2020-04-29T13:30:04.918704",
                "file-date": "2020-11-20T21:01:54.532281",
                "file-name": "/pbench/archive/fs-version-001/dhcp31-187.example.com/fio_rhel8_kvm_perf43_preallocfull_nvme_run4_iothread_isolcpus_2020.04.29T12.49.13.tar.xz",
                "file-size": 216319392,
                "id": "random_md5_string1",
                "iterations": "0__bs=4k_iodepth=1_iodepth_batch_complete_max=1, 1__bs=32k_iodepth=1_iodepth_batch_complete_max=1, 2__bs=256k_iodepth=1_iodepth_batch_complete_max=1, 3__bs=4k_iodepth=8_iodepth_batch_complete_max=8, 4__bs=32k_iodepth=8_iodepth_batch_complete_max=8, 5__bs=256k_iodepth=8_iodepth_batch_complete_max=8, 6__bs=4k_iodepth=16_iodepth_batch_complete_max=16, 7__bs=32k_iodepth=16_iodepth_batch_complete_max=16, 8__bs=256k_iodepth=16_iodepth_batch_complete_max=16",
                "md5": "12fb1e952fd826727810868c9327254f",
                "name": "drb",
                "pbench-agent-version": "0.68-1gf4c94b4d",
                "raw_size": 292124533,
                "script": "fio",
                "start": "2020-04-29T12:49:13.560620",
                "tar-ball-creation-timestamp": "2020-04-29T15:16:51.880540",
                "toc-prefix": "fio_rhel8_kvm_perf43_preallocfull_nvme_run4_iothread_isolcpus_2020.04.29T12.49.13",
                "toolsgroup": "default",
            },
            "serverMetadata": {
                "global.seen": None,
                "server.deletion": "2022-12-26",
            },
        }
        assert expected == res_json

    def test_empty_query(
        self,
        client,
        server_config,
        query_api,
        find_template,
        build_auth_header,
    ):
        """
        Check the handling of a query that doesn't return any data.
        The test will run thrice with different values of the build_auth_header
        fixture.
        """
        expected_status = self.get_expected_status(
            self.payload, build_auth_header["header_param"]
        )

        # In this case, if we don't get a validation/permission error, expect
        # to fail because of the unexpectedly empty Elasticsearch result.
        if expected_status == HTTPStatus.OK:
            expected_status = HTTPStatus.BAD_REQUEST
        index = self.build_index(
            server_config, self.date_range(self.payload["start"], self.payload["end"])
        )
        response = query_api(
            f"{self.pbench_endpoint}",
            self.elastic_endpoint,
            self.payload,
            index,
            expected_status,
            headers=build_auth_header["header"],
            json=self.empty_es_response_payload,
        )
        assert response.status_code == expected_status
        if response.status_code == HTTPStatus.BAD_REQUEST:
            assert response.json["message"].find("dataset has gone missing") != -1

    def test_nonunique_query(self, client, server_config, query_api, find_template):
        """
        Check the handling of a query that returns too much data.
        """
        # We look for public data so we can make an unauthorized query
        del self.payload["user"]
        self.payload["access"] = "public"
        response_payload = {
            "hits": {
                "total": {"value": 0, "relation": "eq"},
                "max_score": None,
                "hits": [{"a": True}, {"b": False}],
            },
        }

        index = self.build_index(server_config, ("2020-08", "2020-09", "2020-10"))
        response = query_api(
            f"{self.pbench_endpoint}",
            self.elastic_endpoint,
            self.payload,
            index,
            HTTPStatus.BAD_REQUEST,
            json=response_payload,
        )
        assert response.json["message"].find("Too many hits for a unique query") != -1
