from http import HTTPStatus
from logging import Logger

from pbench.server import PbenchServerConfig
from pbench.server.api.resources import (
    API_AUTHORIZATION,
    API_METHOD,
    API_OPERATION,
    ApiParams,
    ApiSchema,
    JSON,
    Parameter,
    ParamType,
    Schema,
)
from pbench.server.api.resources.query_apis import CONTEXT, PostprocessError
from pbench.server.api.resources.query_apis.datasets import IndexMapBase


class DatasetsContents(IndexMapBase):
    """
    Datasets Contents API returns the list of sub-directories and files
    present under a directory.
    """

    MAX_SIZE = 10000

    def __init__(self, config: PbenchServerConfig, logger: Logger):
        super().__init__(
            config,
            logger,
            ApiSchema(
                API_METHOD.POST,
                API_OPERATION.READ,
                uri_schema=Schema(
                    Parameter("dataset", ParamType.DATASET, required=True)
                ),
                body_schema=Schema(
                    Parameter("parent", ParamType.STRING, required=True),
                ),
                authorization=API_AUTHORIZATION.DATASET,
            ),
        )

    def assemble(self, params: ApiParams, context: CONTEXT) -> JSON:
        """
        Construct a pbench Elasticsearch query for getting a list of
        documents which contains the user provided parent with files
        and its sub-directories with metadata of run-toc index document
        that belong to the given run id.

        Args:
            params: API parameters
            context: propagate the dataset and the "parent" directory value.

        EXAMPLE:
        {
            "parent": '/1-default'
        }
        """
        # Copy parent directory metadata to CONTEXT for postprocessor
        parent = context["parent"] = params.body.get("parent")
        dataset = context["dataset"]

        self.logger.info(
            "Discover dataset {} Contents, directory {}",
            dataset.name,
            parent,
        )

        # Retrieve the ES indices that belong to this run_id from the metadata
        # table
        indices = self.get_index(dataset, "run-toc")

        return {
            "path": f"/{indices}/_search",
            "kwargs": {
                "json": {
                    "size": self.MAX_SIZE,
                    "query": {
                        "bool": {
                            "filter": [
                                {
                                    "dis_max": {
                                        "queries": [
                                            {"term": {"directory": parent}},
                                            {"term": {"parent": parent}},
                                        ]
                                    }
                                },
                                {"term": {"run_data_parent": dataset.resource_id}},
                            ],
                            "must_not": {"regexp": {"directory": f"{parent}/[^/]+/.+"}},
                        }
                    },
                }
            },
        }

    def postprocess(self, es_json: JSON, context: CONTEXT) -> JSON:
        """
        Returns a Flask Response containing a JSON object (keyword/value
        pairs) whose values are lists of entries describing individual
        directories and files.

        Example: These are the contents of es_json parameter. The
        contents are the result of a request for directory "/1-default"

        {
            "took": 6,
            "timed_out": False,
            "_shards": {"total": 3, "successful": 3, "skipped": 0, "failed": 0},
            "hits": {
                "total": {"value": 2, "relation": "eq"},
                "max_score": 0.0,
                "hits": [
                    {
                        "_index": "riya-pbench.v6.run-toc.2021-05",
                        "_type": "_doc",
                        "_id": "d4a8cc7c4ecef7vshg4tjhrew174828d",
                        "_score": 0.0,
                        "_source": {
                            "parent": "/",
                            "directory": "/1-default",
                            "mtime": "2021-05-01T24:00:00",
                            "mode": "0o755",
                            "name": "1-default",
                            "files": [
                                {
                                    "name": "reference-result",
                                    "mtime": "2021-05-01T24:00:00",
                                    "size": 0,
                                    "mode": "0o777",
                                    "type": "sym",
                                    "linkpath": "sample1",
                                }
                            ],
                            "run_data_parent": "ece030bdgfkjasdkf7435e6a7a6be804",
                            "authorization": {"owner": "1", "access": "private"},
                            "@timestamp": "2021-05-01T24:00:00",
                        },
                    },
                    {
                        "_index": "riya-pbench.v6.run-toc.2021-05",
                        "_type": "_doc",
                        "_id": "3bba25b62fhdgfajgsfdty6797ed06a",
                        "_score": 0.0,
                        "_source": {
                            "parent": "/1-default",
                            "directory": "/1-default/sample1",
                            "mtime": "2021-05-01T24:00:00",
                            "mode": "0o755",
                            "name": "sample1",
                            "ancestor_path_elements": ["1-default"],
                            "files": [
                                {
                                    "name": "result.txt",
                                    "mtime": "2021-05-01T24:00:00",
                                    "size": 0,
                                    "mode": "0o644",
                                    "type": "reg",
                                },
                                {
                                    "name": "user-benchmark.cmd",
                                    "mtime": "2021-05-01T24:00:00",
                                    "size": 114,
                                    "mode": "0o755",
                                    "type": "reg",
                                },
                            ],
                            "run_data_parent": "ece030bdgfkjasdkf7435e6a7a6be804",
                            "authorization": {"owner": "1", "access": "private"},
                            "@timestamp": "2021-05-01T24:00:00",
                        },
                    },
                ],
            },
        }

        Output:
            {
                "directories":
                [
                    "sample1"
                ],
                "files": [
                    {
                        "name": "reference-result",
                        "mtime": "2021-05-01T24:00:00",
                        "size": 0,
                        "mode": "0o777",
                        "type": "sym",
                        "linkpath": "sample1"
                    }
                ]
            }
        """
        if len(es_json["hits"]["hits"]) == 0:
            raise PostprocessError(
                HTTPStatus.NOT_FOUND,
                f"No directory '{context['parent']}' in '{context['dataset']}' contents.",
            )

        dir_list = []
        file_list = []
        for val in es_json["hits"]["hits"]:
            if val["_source"]["directory"] == context["parent"]:
                # Retrieve files list if present else add an empty list.
                file_list = val["_source"].get("files", [])
            elif val["_source"]["parent"] == context["parent"]:
                dir_list.append(val["_source"]["name"])

        return {"directories": dir_list, "files": file_list}
