from http import HTTPStatus

import pytest
import requests

from pbench.server import JSON, PbenchServerConfig
from pbench.server.database.models.datasets import Dataset, DatasetNotFound


class TestDatasetsMetadata:
    @pytest.fixture()
    def query_get_as(self, client, server_config, more_datasets, provide_metadata):
        """
        Helper fixture to perform the API query and validate an expected
        return status.

        Args:
            client: Flask test API client fixture
            server_config: Pbench config fixture
            more_datasets: Dataset construction fixture
            provide_metadata: Dataset metadata fixture
        """

        def query_api(
            ds_name: str, payload: JSON, username: str, expected_status: HTTPStatus
        ) -> requests.Response:
            headers = None
            try:
                dataset = Dataset.query(name=ds_name).resource_id
            except DatasetNotFound:
                dataset = ds_name  # Allow passing deliberately bad value
            if username:
                token = self.token(client, server_config, username)
                headers = {"authorization": f"bearer {token}"}
            response = client.get(
                f"{server_config.rest_uri}/datasets/metadata/{dataset}",
                headers=headers,
                query_string=payload,
            )
            assert response.status_code == expected_status

            # We need to log out to avoid "duplicate auth token" errors on the
            # "put" test which does a PUT followed by two GETs.
            if username:
                client.post(
                    f"{server_config.rest_uri}/logout",
                    headers={"authorization": f"bearer {token}"},
                )
            return response

        return query_api

    @pytest.fixture()
    def query_put_as(self, client, server_config, more_datasets, provide_metadata):
        """
        Helper fixture to perform the API query and validate an expected
        return status.

        Args:
            client: Flask test API client fixture
            server_config: Pbench config fixture
            more_datasets: Dataset construction fixture
            provide_metadata: Dataset metadata fixture
        """

        def query_api(
            ds_name: str, payload: JSON, username: str, expected_status: HTTPStatus
        ) -> requests.Response:
            headers = None
            try:
                dataset = Dataset.query(name=ds_name).resource_id
            except DatasetNotFound:
                dataset = ds_name  # Allow passing deliberately bad value
            if username:
                token = self.token(client, server_config, username)
                headers = {"authorization": f"bearer {token}"}
            response = client.put(
                f"{server_config.rest_uri}/datasets/metadata/{dataset}",
                headers=headers,
                json=payload,
            )
            assert response.status_code == expected_status

            # We need to log out to avoid "duplicate auth token" errors on the
            # test case which does a PUT followed by two GETs.
            if username:
                client.post(
                    f"{server_config.rest_uri}/logout",
                    headers={"authorization": f"bearer {token}"},
                )
            return response

        return query_api

    def token(self, client, config: PbenchServerConfig, user: str) -> str:
        response = client.post(
            f"{config.rest_uri}/login",
            json={"username": user, "password": "12345"},
        )
        assert response.status_code == HTTPStatus.OK
        data = response.json
        assert data["auth_token"]
        return data["auth_token"]

    def test_get_no_dataset(self, query_get_as):
        response = query_get_as(
            "foobar",
            {"metadata": ["global.seen", "global.saved"]},
            "drb",
            HTTPStatus.NOT_FOUND,
        )
        assert response.json == {"message": "Dataset 'foobar' not found"}

    def test_get_bad_keys(self, query_get_as):
        response = query_get_as(
            "drb",
            {"metadata": ["xyzzy", "plugh", "dataset.owner", "dataset.access"]},
            "drb",
            HTTPStatus.BAD_REQUEST,
        )
        assert response.json == {
            "message": "Unrecognized list values ['plugh', 'xyzzy'] given for parameter metadata; expected ['dataset', 'global', 'server', 'user']"
        }

    def test_get1(self, query_get_as):
        response = query_get_as(
            "drb",
            {
                "metadata": ["global.seen", "server", "dataset.access"],
            },
            "drb",
            HTTPStatus.OK,
        )
        assert response.json == {
            "global.seen": None,
            "server": {
                "deletion": "2022-12-26",
                "index-map": {
                    "unit-test.v6.run-data.2020-08": ["random_md5_string1"],
                    "unit-test.v5.result-data-sample.2020-08": ["random_document_uuid"],
                    "unit-test.v6.run-toc.2020-05": ["random_md5_string1"],
                },
            },
            "dataset.access": "private",
        }

    def test_get2(self, query_get_as):
        response = query_get_as(
            "drb",
            {
                "metadata": "global.seen,server.deletion,dataset",
            },
            "drb",
            HTTPStatus.OK,
        )
        assert response.json == {
            "global.seen": None,
            "server.deletion": "2022-12-26",
            "dataset": {
                "access": "private",
                "created": "2020-02-15T00:00:00+00:00",
                "name": "drb",
                "owner": "drb",
                "state": "Indexed",
                "transition": "1970-01-01T00:42:00+00:00",
                "uploaded": "2022-01-01T00:00:00+00:00",
                "metalog": {
                    "pbench": {
                        "config": "test1",
                        "date": "2020-02-15T00:00:00",
                        "name": "drb",
                        "script": "unit-test",
                    },
                    "run": {"controller": "node1.example.com"},
                },
            },
        }

    def test_get3(self, query_get_as):
        response = query_get_as(
            "drb",
            {
                "metadata": [
                    "global.seen",
                    "server.deletion,dataset.access",
                    "user.favorite",
                ],
            },
            "drb",
            HTTPStatus.OK,
        )
        assert response.json == {
            "global.seen": None,
            "server.deletion": "2022-12-26",
            "dataset.access": "private",
            "user.favorite": None,
        }

    def test_get_private_noauth(self, query_get_as):
        response = query_get_as(
            "drb",
            {
                "metadata": [
                    "global.seen",
                    "server.deletion,dataset.access",
                    "user",
                ]
            },
            "test",
            HTTPStatus.FORBIDDEN,
        )
        assert (
            response.json["message"]
            == "User test is not authorized to READ a resource owned by drb with private access"
        )

    def test_get_unauth(self, query_get_as):
        response = query_get_as(
            "drb",
            {
                "metadata": [
                    "global.seen",
                    "server.deletion,dataset.access",
                    "user",
                ],
            },
            None,
            HTTPStatus.UNAUTHORIZED,
        )
        assert (
            response.json["message"]
            == "Unauthenticated client is not authorized to READ a resource owned by drb with private access"
        )

    def test_get_bad_query(self, query_get_as):
        response = query_get_as(
            "drb",
            {
                "controller": "foobar",
                "metadata": "global.seen,server.deletion,dataset.access",
            },
            "drb",
            HTTPStatus.BAD_REQUEST,
        )
        assert response.json == {"message": "Unknown URL query keys: controller"}

    def test_get_bad_query_2(self, query_get_as):
        response = query_get_as(
            "drb",
            {
                "controller": "foobar",
                "plugh": "xyzzy",
                "metadata": ["global.seen", "server.deletion", "dataset.access"],
            },
            "drb",
            HTTPStatus.BAD_REQUEST,
        )
        assert response.json == {"message": "Unknown URL query keys: controller,plugh"}

    @pytest.mark.parametrize("uri", ("/datasets/metadata/", "/datasets/metadata"))
    def test_put_missing_uri_param(self, client, server_config, pbench_token, uri):
        """
        Test behavior when no dataset name is given on the URI. (NOTE that
        Flask automatically handles this with a NOT_FOUND response.)
        """
        response = client.put(f"{server_config.rest_uri}{uri}")
        assert response.status_code == HTTPStatus.NOT_FOUND

    def test_put_missing_key(self, client, server_config, pbench_token):
        """
        Test behavior when JSON payload does not contain all required keys.

        Note that Pbench will silently ignore any additional keys that are
        specified but not required.
        """
        response = client.put(
            f"{server_config.rest_uri}/datasets/metadata/drb", json={}
        )
        assert response.status_code == HTTPStatus.BAD_REQUEST
        assert response.json.get("message") == "Missing required parameters: metadata"

    def test_put_no_dataset(self, client, server_config, attach_dataset):
        response = client.put(
            f"{server_config.rest_uri}/datasets/metadata/foobar",
            json={"metadata": {"global.seen": True, "global.saved": False}},
        )
        assert response.status_code == HTTPStatus.NOT_FOUND
        assert response.json == {"message": "Dataset 'foobar' not found"}

    def test_put_bad_keys(self, client, server_config, attach_dataset):
        response = client.put(
            f"{server_config.rest_uri}/datasets/metadata/drb",
            json={
                "metadata": {"xyzzy": "private", "what": "sup", "global.saved": True}
            },
        )
        assert response.status_code == HTTPStatus.BAD_REQUEST
        assert response.json == {
            "message": "Unrecognized JSON keys ['what', 'xyzzy'] given for parameter metadata; allowed namespaces are ['dataset.name', 'global', 'server.deletion', 'user']"
        }

    def test_put_reserved_metadata(self, client, server_config, attach_dataset):
        response = client.put(
            f"{server_config.rest_uri}/datasets/metadata/drb",
            json={"metadata": {"dataset.access": "private"}},
        )
        assert response.status_code == HTTPStatus.BAD_REQUEST
        assert response.json == {
            "message": "Unrecognized JSON key ['dataset.access'] given for parameter metadata; allowed namespaces are ['dataset.name', 'global', 'server.deletion', 'user']"
        }

    def test_put_nowrite(self, query_get_as, query_put_as):
        response = query_put_as(
            "fio_1",
            {"metadata": {"global.seen": False, "global.saved": True}},
            "test",
            HTTPStatus.FORBIDDEN,
        )
        assert (
            response.json["message"]
            == "User test is not authorized to UPDATE a resource owned by drb with public access"
        )

    def test_put_noauth(self, query_get_as, query_put_as):
        response = query_put_as(
            "fio_1",
            {"metadata": {"global.seen": False, "global.saved": True}},
            None,
            HTTPStatus.UNAUTHORIZED,
        )
        assert (
            response.json["message"]
            == "Unauthenticated client is not authorized to UPDATE a resource owned by drb with public access"
        )

    def test_put_invalid_name(self, query_get_as, query_put_as):
        """
        Test that invalid special values for dataset.name are detected before
        any metadata is changed and that we fail with BAD_REQUEST rather than
        with an internal error.
        """
        put = query_put_as(
            "drb",
            {
                "metadata": {
                    "global.dashboard.c": 1,
                    "dataset.name": 1,
                    "global.dashboard.test": "A",
                }
            },
            "drb",
            HTTPStatus.BAD_REQUEST,
        )
        assert (
            put.json["message"]
            == "Metadata key 'dataset.name' value 1 for dataset drb(3)|drb must be a UTF-8 string of 1 to 32 characters"
        )

        # verify that the values didn't change
        get = query_get_as(
            "drb",
            {"metadata": "global.dashboard.c,dataset.name,global.dashboard.test"},
            "drb",
            HTTPStatus.OK,
        )
        assert get.json == {
            "dataset.name": "drb",
            "global.dashboard.test": None,
            "global.dashboard.c": None,
        }

    def test_put_invalid_deletion(self, query_get_as, query_put_as):
        """
        Test that invalid special values for server.deletion are detected
        before any metadata is changed and that we fail with BAD_REQUEST rather
        than with an internal error.
        """
        put = query_put_as(
            "drb",
            {
                "metadata": {
                    "user.one": 2,
                    "server.deletion": "1800-25-55",
                    "user.dashboard.test": "B",
                }
            },
            "drb",
            HTTPStatus.BAD_REQUEST,
        )
        assert (
            put.json["message"]
            == "Metadata key 'server.deletion' value '1800-25-55' for dataset drb(3)|drb must be a date/time"
        )

        # verify that the values didn't change
        get = query_get_as(
            "drb",
            {"metadata": "server.deletion,user.dashboard.test,user.one"},
            "drb",
            HTTPStatus.OK,
        )
        assert get.json == {
            "server.deletion": "2022-12-26",
            "user.dashboard.test": None,
            "user.one": None,
        }

    def test_put(self, query_get_as, query_put_as):
        response = query_put_as(
            "drb",
            {"metadata": {"global.seen": False, "global.saved": True}},
            "drb",
            HTTPStatus.OK,
        )
        assert response.json == {"global.saved": True, "global.seen": False}
        response = query_get_as(
            "drb", {"metadata": "global,dataset.access"}, "drb", HTTPStatus.OK
        )
        assert response.json == {
            "global": {"contact": "me@example.com", "saved": True, "seen": False},
            "dataset.access": "private",
        }

        # Try a second GET, returning "global" fields separately:
        response = query_get_as(
            "drb",
            {"metadata": ["global.seen", "global.saved", "dataset.access"]},
            "drb",
            HTTPStatus.OK,
        )
        assert response.json == {
            "global.saved": True,
            "global.seen": False,
            "dataset.access": "private",
        }

    def test_put_user(self, query_get_as, query_put_as):
        response = query_put_as(
            "fio_1",
            {"metadata": {"user.favorite": True, "user.tag": "AWS"}},
            "drb",
            HTTPStatus.OK,
        )
        assert response.json == {"user.favorite": True, "user.tag": "AWS"}
        response = query_put_as(
            "fio_1",
            {"metadata": {"user.favorite": False, "user.tag": "RHEL"}},
            "test",
            HTTPStatus.OK,
        )
        assert response.json == {"user.favorite": False, "user.tag": "RHEL"}
        response = query_put_as(
            "fio_1",
            {"metadata": {"user.favorite": False, "user.tag": "BAD"}},
            None,
            HTTPStatus.UNAUTHORIZED,
        )

        response = query_get_as("fio_1", {"metadata": "user"}, "drb", HTTPStatus.OK)
        assert response.json == {"user": {"favorite": True, "tag": "AWS"}}
        response = query_get_as("fio_1", {"metadata": "user"}, "test", HTTPStatus.OK)
        assert response.json == {"user": {"favorite": False, "tag": "RHEL"}}
        response = query_get_as("fio_1", {"metadata": "user"}, None, HTTPStatus.OK)
        assert response.json == {"user": None}
