"""SSH transport (developer mode): reads and writes xochitl's files directly on the tablet.

Uses the system `ssh` client. Writes restart xochitl so the UI picks them up.
"""

from __future__ import annotations

import asyncio
import io
import json
import shlex
import tarfile

from .base import RemoteDoc, RmDoc, check_file_name, document_metadata, new_uuid, now_ms, pdf_content

XOCHITL = "/home/root/.local/share/remarkable/xochitl"


# Exit code the remote commands use for "no such document or file", so it is not mistaken for an ssh failure.
MISSING = 44


class SshError(RuntimeError):
    def __init__(self, message: str, returncode: int | None = None):
        super().__init__(message)
        self.returncode = returncode


class SshTransport:
    def __init__(self, host: str, key_path: str = "", user: str = "root"):
        self.target = f"{user}@{host}"
        self.key_path = key_path

    async def aclose(self) -> None:
        pass

    async def _ssh(self, command: str, stdin: bytes | None = None) -> bytes:
        args = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
        if self.key_path:
            args += ["-i", self.key_path]
        proc = await asyncio.create_subprocess_exec(
            *args, self.target, command,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate(stdin)
        if proc.returncode != 0:
            raise SshError(f"ssh {command!r} failed: {err.decode()[:300]}", proc.returncode)
        return out

    async def _all_metadata(self) -> dict[str, dict]:
        # One JSON object per line: {"uuid": ..., "meta": {...}}
        script = (
            f"cd {XOCHITL} && for f in *.metadata; do "
            'printf \'{"uuid":"%s","meta":\' "${f%.metadata}"; tr -d "\\n" < "$f"; echo "}"; done'
        )
        out = await self._ssh(script)
        docs = {}
        for line in out.decode().splitlines():
            if line.strip():
                item = json.loads(line)
                docs[item["uuid"]] = item["meta"]
        return {u: m for u, m in docs.items() if not m.get("deleted") and m.get("parent") != "trash"}

    async def _write_files(self, files: dict[str, bytes]) -> None:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        await self._ssh(f"tar -x -C {XOCHITL}", buf.getvalue())

    async def _restart(self) -> None:
        await self._ssh("systemctl restart xochitl")

    def _folder(self, name: str, docs: dict[str, dict]) -> str | None:
        for u, m in docs.items():
            if m.get("type") == "CollectionType" and m.get("visibleName") == name and m.get("parent", "") == "":
                return u
        return None

    async def ensure_folder(self, name: str) -> str:
        docs = await self._all_metadata()
        found = self._folder(name, docs)
        if found:
            return found
        u = new_uuid()
        await self._write_files({
            f"{u}.metadata": json.dumps(document_metadata(name, "", "CollectionType")).encode(),
            f"{u}.content": b"{}",
        })
        await self._restart()
        return u

    async def list_folder(self, name: str) -> list[RemoteDoc]:
        docs = await self._all_metadata()
        folder = self._folder(name, docs)
        if folder is None:
            return []
        return [
            RemoteDoc(u, m.get("visibleName", ""), folder, version=str(m.get("lastModified", "")),
                      last_modified=int(m.get("lastModified", "0") or 0) / 1000)
            for u, m in docs.items()
            if m.get("parent") == folder and m.get("type") == "DocumentType"
        ]

    async def upload_pdf(self, pdf: bytes, visible_name: str, folder: str) -> str:
        import pymupdf

        folder_uuid = await self.ensure_folder(folder)
        with pymupdf.open(stream=pdf, filetype="pdf") as d:
            pages = d.page_count
        u = new_uuid()
        await self._write_files({
            f"{u}.metadata": json.dumps(document_metadata(visible_name, folder_uuid)).encode(),
            f"{u}.content": json.dumps(pdf_content(pages)).encode(),
            f"{u}.pdf": pdf,
        })
        await self._restart()
        return u

    async def download(self, doc: RemoteDoc) -> RmDoc:
        u = shlex.quote(doc.uuid)
        try:
            out = await self._ssh(f"cd {XOCHITL} && {{ test -f {u}.metadata || exit {MISSING}; }} "
                                  f"&& tar -c {u}.* $(test -d {u} && echo {u})")
        except SshError as e:
            if e.returncode == MISSING:
                raise FileNotFoundError(f"document {doc.uuid} not found") from e
            raise
        files = {}
        with tarfile.open(fileobj=io.BytesIO(out)) as tar:
            for m in tar.getmembers():
                if m.isfile():
                    files[m.name] = tar.extractfile(m).read()
        return RmDoc(uuid=doc.uuid, files=files)

    async def download_file(self, doc_uuid: str, name: str) -> bytes:
        check_file_name(doc_uuid, name)
        path = shlex.quote(f"{XOCHITL}/{name}")
        try:
            return await self._ssh(f"test -f {path} || exit {MISSING}; cat {path}")
        except SshError as e:
            if e.returncode == MISSING:
                raise FileNotFoundError(f"document {doc_uuid} has no file {name}") from e
            raise

    async def move(self, doc: RemoteDoc, folder: str) -> None:
        folder_uuid = await self.ensure_folder(folder)
        u = shlex.quote(doc.uuid)
        meta = json.loads(await self._ssh(f"cat {XOCHITL}/{u}.metadata"))
        meta.update(parent=folder_uuid, lastModified=now_ms(), metadatamodified=True)
        await self._write_files({f"{doc.uuid}.metadata": json.dumps(meta).encode()})
        await self._restart()
