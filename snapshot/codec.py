"""Typed JSONL, never pickle; atomic completion manifests and SHA256."""

import base64
import datetime as dt
import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path


def encode(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"t": "bytes", "v": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Decimal):
        return {"t": "decimal", "v": str(value)}
    if isinstance(value, dt.datetime):
        return {"t": "datetime", "v": value.isoformat(" ")}
    if isinstance(value, dt.date):
        return {"t": "date", "v": value.isoformat()}
    if isinstance(value, dt.timedelta):
        return {"t": "timedelta", "v": (value.days * 86400 + value.seconds) * 1000000 + value.microseconds}
    if isinstance(value, dt.time):
        return {"t": "time", "v": value.isoformat()}
    raise TypeError(f"지원하지 않는 값 타입: {type(value).__name__}")


def decode(value):
    if not isinstance(value, dict):
        return value
    converters = {
        "bytes": base64.b64decode,
        "decimal": Decimal,
        "datetime": dt.datetime.fromisoformat,
        "date": dt.date.fromisoformat,
        "time": dt.time.fromisoformat,
        "timedelta": lambda v: dt.timedelta(microseconds=v),
    }
    return converters[value["t"]](value["v"])


class Writer:
    def __init__(self, path):
        self.path = Path(path)
        self.partial = self.path.with_suffix(".partial")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fp = self.partial.open("wb")
        self.partial.chmod(0o600)
        self.digest = hashlib.sha256()
        self.rows = self.size = 0

    def write(self, rows):
        for row in rows:
            line = (json.dumps([encode(v) for v in row], ensure_ascii=False, allow_nan=False) + "\n").encode(
                "utf8"
            )
            self.fp.write(line)
            self.digest.update(line)
            self.rows += 1
            self.size += len(line)

    def complete(self, metadata):
        self.fp.flush()
        os.fsync(self.fp.fileno())
        self.fp.close()
        os.replace(self.partial, self.path)
        manifest = dict(
            metadata, complete=True, rows=self.rows, bytes=self.size, sha256=self.digest.hexdigest()
        )
        tmp = self.path.with_suffix(".manifest.tmp")
        with tmp.open("w", encoding="utf8") as fp:
            tmp.chmod(0o600)
            json.dump(manifest, fp, ensure_ascii=False)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, self.path.with_suffix(".manifest.json"))
        return manifest

    def close(self):
        self.fp.close()


def verify(path, expected, check=lambda: None):
    path = Path(path)
    m = json.loads(path.with_suffix(".manifest.json").read_text(encoding="utf8"))
    if not m.get("complete") or any(m.get(k) != v for k, v in expected.items()):
        raise ValueError("완료 파일의 실행 설정/스키마가 일치하지 않습니다.")
    digest = hashlib.sha256()
    rows = size = 0
    with path.open("rb") as fp:
        for line in fp:
            check()
            digest.update(line)
            rows += 1
            size += len(line)
    if (digest.hexdigest(), rows, size) != (m["sha256"], m["rows"], m["bytes"]):
        raise ValueError("중간 파일 무결성 검증 실패")
    return m


def batches(path, count, max_bytes=2 * 1024 * 1024):
    rows, size = [], 0
    with Path(path).open("rb") as fp:
        for line in fp:
            rows.append(tuple(decode(v) for v in json.loads(line)))
            size += len(line)
            if len(rows) >= count or size >= max_bytes:
                yield rows
                rows, size = [], 0
    if rows:
        yield rows
