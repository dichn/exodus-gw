import mock
import pytest
import textwrap

from fastapi import HTTPException

from exodus_gw.s3.api import multipart_upload, abort_multipart_upload, upload
from exodus_gw.s3.util import xml_response

TEST_KEY = "b5bb9d8014a0f9b1d61e21e796d78dccdf1352f23cd32812f4850b878ae4944c"


@pytest.fixture(autouse=True)
def mock_s3_client():
    with mock.patch("aioboto3.client") as mock_client:
        s3_client = mock.AsyncMock()
        s3_client.__aenter__.return_value = s3_client
        mock_client.return_value = s3_client
        yield s3_client


@pytest.fixture()
def mock_request_reader():
    # We don't use the real request reader for these tests as it becomes
    # rather complicated to verify that boto methods were called with the
    # correct expected value. The class is tested separately.
    with mock.patch("exodus_gw.s3.util.RequestReader.get_reader") as m:
        yield m


@pytest.mark.asyncio
async def test_full_upload(mock_s3_client, mock_request_reader):
    """Uploading a complete object is delegated correctly to S3."""

    mock_s3_client.put_object.return_value = {
        "ETag": "a1b2c3",
    }

    request = mock.Mock(
        headers={
            "Content-MD5": "9d0568469d206c1aedf1b71f12f474bc",
            "Content-Length": "10",
        }
    )
    mock_request_reader.return_value = b"some bytes"

    response = await upload(
        request=request,
        bucket="my-bucket",
        key=TEST_KEY,
        uploadId=None,
        partNumber=None,
    )

    # It should delegate request to real S3
    mock_s3_client.put_object.assert_called_once_with(
        Bucket="my-bucket",
        Key=TEST_KEY,
        Body=b"some bytes",
        ContentMD5="9d0568469d206c1aedf1b71f12f474bc",
        ContentLength=10,
    )

    # It should succeed
    assert response.status_code == 200

    # It should return the ETag
    assert response.headers["ETag"] == "a1b2c3"

    # It should have an empty body
    assert response.body == b""


@pytest.mark.asyncio
async def test_part_upload(mock_s3_client, mock_request_reader):
    """Uploading part of an object is delegated correctly to S3."""

    mock_s3_client.upload_part.return_value = {
        "ETag": "aabbcc",
    }

    request = mock.Mock(
        headers={
            "Content-MD5": "e8b7c279de413b7b15f44bf71a796f95",
            "Content-Length": "10",
        }
    )
    mock_request_reader.return_value = b"best bytes"

    response = await upload(
        request=request,
        bucket="my-bucket",
        key=TEST_KEY,
        uploadId="my-best-upload",
        partNumber=88,
    )

    # It should delegate request to real S3
    mock_s3_client.upload_part.assert_called_once_with(
        Bucket="my-bucket",
        Key=TEST_KEY,
        Body=b"best bytes",
        PartNumber=88,
        UploadId="my-best-upload",
        ContentMD5="e8b7c279de413b7b15f44bf71a796f95",
        ContentLength=10,
    )

    # It should succeed
    assert response.status_code == 200

    # It should return the ETag
    assert response.headers["ETag"] == "aabbcc"

    # It should have an empty body
    assert response.body == b""
