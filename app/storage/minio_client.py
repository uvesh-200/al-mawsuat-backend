import asyncio
import io
from typing import Optional

from minio import Minio
from minio.error import S3Error

from app.config import settings


class StorageClient:
    def __init__(self) -> None:
        self._client = Minio(
            settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            secure=False,
        )

    async def ensure_buckets(self) -> None:
        try:
            async with asyncio.timeout(15):
                for bucket in (settings.MINIO_BUCKET_BOOKS, settings.MINIO_BUCKET_HIGHLIGHTS):
                    exists = await asyncio.to_thread(
                        self._client.bucket_exists, bucket
                    )
                    if not exists:
                        await asyncio.to_thread(self._client.make_bucket, bucket)
        except (TimeoutError, asyncio.TimeoutError):
            pass

    async def upload_file(
        self, bucket: str, path: str, data: bytes, content_type: str
    ) -> str:
        await asyncio.to_thread(
            self._client.put_object,
            bucket,
            path,
            io.BytesIO(data),
            length=len(data),
            content_type=content_type,
        )
        return path

    async def get_file(self, bucket: str, path: str) -> bytes:
        try:
            response = await asyncio.to_thread(
                self._client.get_object, bucket, path
            )
            data = response.read()
            response.close()
            response.release_conn()
            return data
        except S3Error as exc:
            if exc.code == "NoSuchKey":
                raise FileNotFoundError(
                    f"File not found: {bucket}/{path}"
                ) from exc
            raise

    async def get_file_safe(self, bucket: str, path: str) -> Optional[bytes]:
        try:
            return await self.get_file(bucket, path)
        except FileNotFoundError:
            return None

    async def delete_file(self, bucket: str, path: str) -> None:
        try:
            await asyncio.to_thread(self._client.remove_object, bucket, path)
        except S3Error:
            pass


storage = StorageClient()
