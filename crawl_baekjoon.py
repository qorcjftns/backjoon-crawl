#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag
from markdownify import markdownify as md


BASE_URL = "https://www.acmicpc.net"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; baekjoon-archiver/1.0; "
    "+https://github.com/cheulsoonbaek/baekjoon-crawl)"
)


@dataclass
class CrawlConfig:
    output_dir: Path
    delay: float
    timeout: float
    retries: int
    start_id: int | None
    end_id: int | None
    user_agent: str
    save_html: bool
    only_ids: list[int] | None
    workers: int
    retry_log_input: Path | None
    failure_log_path: Path | None


@dataclass
class FailedRequestRecord:
    resource_type: str
    url: str
    problem_id: int | None
    output_path: str | None
    reason: str
    attempts: int
    status_code: int | None = None
    selector: str | None = None
    source_problem_id: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "resource_type": self.resource_type,
            "url": self.url,
            "problem_id": self.problem_id,
            "output_path": self.output_path,
            "reason": self.reason,
            "attempts": self.attempts,
            "status_code": self.status_code,
            "selector": self.selector,
            "source_problem_id": self.source_problem_id,
            "failed_at": datetime.now(timezone.utc).isoformat(),
        }


class RequestFailedError(RuntimeError):
    def __init__(
        self,
        *,
        url: str,
        attempts: int,
        reason: str,
        status_code: int | None = None,
    ) -> None:
        self.url = url
        self.attempts = attempts
        self.reason = reason
        self.status_code = status_code
        super().__init__(f"request failed for {url}: {reason}")


class BaekjoonCrawler:
    def __init__(self, config: CrawlConfig) -> None:
        self.config = config
        self._session_local = threading.local()
        self._manifest_lock = threading.Lock()
        self._failure_log_lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._next_request_at = 0.0
        self.output_dir = config.output_dir
        self.problems_dir = self.output_dir / "problems"
        self.assets_dir = self.output_dir / "assets"
        self.html_dir = self.output_dir / "html"
        self.manifest_path = self.output_dir / "manifest.jsonl"
        self.failure_log_path = config.failure_log_path or (self.output_dir / "failed_requests.jsonl")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.failure_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.problems_dir.mkdir(parents=True, exist_ok=True)
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        if config.save_html:
            self.html_dir.mkdir(parents=True, exist_ok=True)
        self._manifest_problem_ids = self._load_manifest_problem_ids()

    def _load_manifest_problem_ids(self) -> set[int]:
        problem_ids: set[int] = set()
        if not self.manifest_path.exists():
            return problem_ids

        with self.manifest_path.open("r", encoding="utf-8") as fp:
            for line_number, line in enumerate(fp, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    print(
                        f"[warn] invalid manifest line {line_number}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                if not isinstance(record, dict):
                    continue
                problem_id = parse_optional_int(record.get("problem_id"))
                if problem_id is not None:
                    problem_ids.add(problem_id)
        return problem_ids

    def _get_session(self) -> requests.Session:
        session = getattr(self._session_local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": self.config.user_agent,
                    "Accept-Language": "ko,en-US;q=0.9,en;q=0.8",
                }
            )
            self._session_local.session = session
        return session

    def request(self, url: str, *, stream: bool = False) -> requests.Response:
        session = self._get_session()
        last_reason = "unknown"
        last_status_code: int | None = None
        for attempt in range(1, self.config.retries + 1):
            try:
                self._wait_for_request_slot()
                response = session.get(
                    url,
                    timeout=self.config.timeout,
                    stream=stream,
                )
                if 200 <= response.status_code < 300:
                    return response

                last_status_code = response.status_code
                last_reason = f"HTTP {response.status_code}"
                response.close()
                if attempt < self.config.retries:
                    print(
                        f"[warn] request failed ({attempt}/{self.config.retries}) "
                        f"for {url}: {last_reason}",
                        file=sys.stderr,
                        flush=True,
                    )
                    time.sleep(self.config.delay * attempt)
                    continue
                break
            except Exception as exc:  # noqa: BLE001
                last_reason = str(exc)
                last_status_code = None
                wait_seconds = self.config.delay * attempt
                print(
                    f"[warn] request failed ({attempt}/{self.config.retries}) "
                    f"for {url}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(wait_seconds)

        raise RequestFailedError(
            url=url,
            attempts=self.config.retries,
            reason=last_reason,
            status_code=last_status_code,
        )

    def _wait_for_request_slot(self) -> None:
        if self.config.delay <= 0:
            return

        while True:
            with self._request_lock:
                now = time.monotonic()
                if now >= self._next_request_at:
                    self._next_request_at = now + self.config.delay
                    return
                sleep_seconds = self._next_request_at - now
            time.sleep(sleep_seconds)

    def paginate_problem_ids(self) -> list[int]:
        if self.config.only_ids:
            return self._apply_id_filters(self.config.only_ids)
        if self.config.start_id is None:
            raise ValueError("--start-id or --only-ids is required")
        if self.config.end_id is None:
            raise ValueError("--end-id or --only-ids is required")
        if self.config.end_id < self.config.start_id:
            raise ValueError("--end-id must be greater than or equal to --start-id")

        problem_ids = list(range(self.config.start_id, self.config.end_id + 1))
        print(
            f"[info] generated {len(problem_ids)} problem ids from "
            f"{self.config.start_id} to {self.config.end_id}",
            flush=True,
        )
        return problem_ids

    def load_retry_records(self) -> list[dict[str, object]]:
        if self.config.retry_log_input is None:
            return []

        records: list[dict[str, object]] = []
        with self.config.retry_log_input.open("r", encoding="utf-8") as fp:
            for line_number, line in enumerate(fp, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    print(
                        f"[warn] invalid retry log line {line_number}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("resource_type") not in {"problem", "image"}:
                    continue
                if not record.get("url"):
                    continue
                records.append(record)

        print(
            f"[info] loaded {len(records)} retry records from {self.config.retry_log_input}",
            flush=True,
        )
        return records

    def crawl_all(self) -> None:
        if self.config.retry_log_input is not None:
            self.retry_failed_requests()
            return

        problem_ids = self.paginate_problem_ids()
        total = len(problem_ids)
        print(
            "[info] config: "
            f"workers={self.config.workers}, "
            f"start_id={self.config.start_id}, "
            f"end_id={self.config.end_id}, "
            f"delay={self.config.delay}, "
            f"timeout={self.config.timeout}",
            flush=True,
        )
        pending: list[tuple[int, int]] = []
        for index, problem_id in enumerate(problem_ids, start=1):
            output_path = self.problems_dir / f"{problem_id}.md"
            if output_path.exists():
                print(f"[skip] {problem_id} already exists ({index}/{total})", flush=True)
                continue
            pending.append((index, problem_id))

        print(f"[info] pending problems: {len(pending)}", flush=True)

        if self.config.workers <= 1:
            for index, problem_id in pending:
                try:
                    self.crawl_problem(problem_id, index=index, total=total)
                except Exception as exc:  # noqa: BLE001
                    print(f"[error] failed to crawl {problem_id}: {exc}", file=sys.stderr, flush=True)
            return

        print(f"[info] starting thread pool with {self.config.workers} workers", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.config.workers) as executor:
            future_map = {
                executor.submit(self.crawl_problem, problem_id, index=index, total=total): problem_id
                for index, problem_id in pending
            }
            for future in concurrent.futures.as_completed(future_map):
                problem_id = future_map[future]
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001
                    print(f"[error] failed to crawl {problem_id}: {exc}", file=sys.stderr, flush=True)

    def retry_failed_requests(self) -> None:
        records = self.load_retry_records()
        total = len(records)
        print(
            "[info] retry config: "
            f"workers={self.config.workers}, "
            f"delay={self.config.delay}, "
            f"timeout={self.config.timeout}, "
            f"failure_log={self.failure_log_path}",
            flush=True,
        )
        if total == 0:
            print("[info] no retry records to process", flush=True)
            return

        if self.config.workers <= 1:
            for index, record in enumerate(records, start=1):
                self.retry_record(record, index=index, total=total)
            return

        print(f"[info] starting retry thread pool with {self.config.workers} workers", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.config.workers) as executor:
            future_map = {
                executor.submit(self.retry_record, record, index=index, total=total): record
                for index, record in enumerate(records, start=1)
            }
            for future in concurrent.futures.as_completed(future_map):
                record = future_map[future]
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"[error] retry failed unexpectedly for {record.get('url')}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )

    def crawl_problem(self, problem_id: int, *, index: int, total: int) -> None:
        url = f"{BASE_URL}/problem/{problem_id}"
        output_path = self.problems_dir / f"{problem_id}.md"
        if output_path.exists():
            print(
                f"[skip] problem file already exists for {problem_id}: {output_path}",
                flush=True,
            )
            return
        print(
            f"[info] crawling {problem_id} ({index}/{total}) "
            f"[thread={threading.current_thread().name}]: {url}",
            flush=True,
        )
        try:
            response = self.request(url)
        except RequestFailedError as exc:
            self.log_failure(
                FailedRequestRecord(
                    resource_type="problem",
                    url=url,
                    problem_id=problem_id,
                    output_path=str(output_path.relative_to(self.output_dir)),
                    reason=exc.reason,
                    attempts=exc.attempts,
                    status_code=exc.status_code,
                )
            )
            print(f"[error] problem request failed for {problem_id}: {exc.reason}", file=sys.stderr, flush=True)
            return

        try:
            if self.config.save_html:
                (self.html_dir / f"{problem_id}.html").write_text(
                    response.text,
                    encoding="utf-8",
                )

            soup = BeautifulSoup(response.text, "html.parser")
            title = text_or_none(soup.select_one("#problem_title")) or f"문제 {problem_id}"
            metadata = self._extract_metadata(soup)
            tags = self._extract_tags(soup)

            asset_dir = self.assets_dir / str(problem_id)
            asset_dir.mkdir(parents=True, exist_ok=True)

            sections: list[tuple[str, str]] = []
            for label, selector in (
                ("문제", "#problem_description"),
                ("입력", "#problem_input"),
                ("출력", "#problem_output"),
                ("힌트", "#problem_hint"),
            ):
                section = soup.select_one(selector)
                if not section:
                    continue
                self._download_embedded_assets(section, asset_dir, problem_id)
                sections.append((label, self._html_to_markdown(section)))

            for sample_name, sample_markdown in self._extract_samples(soup):
                sections.append((sample_name, sample_markdown))

            markdown = self._render_markdown(
                problem_id=problem_id,
                title=title,
                sections=sections,
            )
            output_path.write_text(markdown, encoding="utf-8")

            manifest_record = {
                "problem_id": problem_id,
                "title": title,
                "url": url,
                "path": str(output_path.relative_to(self.output_dir)),
                "assets_dir": str(asset_dir.relative_to(self.output_dir)),
                "tags": tags,
                **metadata,
            }
            self.write_manifest_record(manifest_record)
        except Exception as exc:  # noqa: BLE001
            self.log_failure(
                FailedRequestRecord(
                    resource_type="problem",
                    url=url,
                    problem_id=problem_id,
                    output_path=str(output_path.relative_to(self.output_dir)),
                    reason=f"processing error: {exc}",
                    attempts=0,
                )
            )
            print(f"[error] problem processing failed for {problem_id}: {exc}", file=sys.stderr, flush=True)

    def _apply_id_filters(self, problem_ids: Iterable[int]) -> list[int]:
        return sorted(
            {
                problem_id
                for problem_id in problem_ids
                if (
                    (self.config.start_id is None or problem_id >= self.config.start_id)
                    and (self.config.end_id is None or problem_id <= self.config.end_id)
                )
            }
        )

    def log_failure(self, record: FailedRequestRecord) -> None:
        with self._failure_log_lock:
            with self.failure_log_path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")

    def write_manifest_record(self, manifest_record: dict[str, object]) -> None:
        problem_id = parse_optional_int(manifest_record.get("problem_id"))
        if problem_id is None:
            return

        with self._manifest_lock:
            if problem_id in self._manifest_problem_ids:
                return
            with self.manifest_path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(manifest_record, ensure_ascii=False) + "\n")
            self._manifest_problem_ids.add(problem_id)

    def retry_record(self, record: dict[str, object], *, index: int, total: int) -> None:
        resource_type = str(record.get("resource_type", ""))
        url = str(record.get("url", ""))
        print(
            f"[info] retrying {resource_type} ({index}/{total}) "
            f"[thread={threading.current_thread().name}]: {url}",
            flush=True,
        )

        if resource_type == "problem":
            problem_id = parse_problem_id(url, record.get("problem_id"))
            if problem_id is None:
                print(f"[error] retry record missing valid problem id for {url}", file=sys.stderr, flush=True)
                return
            self.crawl_problem(problem_id, index=index, total=total)
            return

        if resource_type == "image":
            output_path_value = record.get("output_path")
            if not output_path_value:
                print(f"[error] retry image record missing output_path for {url}", file=sys.stderr, flush=True)
                return
            output_path = self.output_dir / str(output_path_value)
            source_problem_id = parse_optional_int(record.get("source_problem_id"))
            self.retry_image(
                url,
                output_path=output_path,
                source_problem_id=source_problem_id,
            )
            return

        print(f"[warn] unsupported retry record type: {resource_type}", file=sys.stderr, flush=True)

    def retry_image(self, url: str, *, output_path: Path, source_problem_id: int | None) -> None:
        if output_path.exists():
            print(f"[skip] image file already exists: {output_path}", flush=True)
            return
        try:
            self._download_binary(url, output_path)
        except RequestFailedError as exc:
            self.log_failure(
                FailedRequestRecord(
                    resource_type="image",
                    url=url,
                    problem_id=source_problem_id,
                    source_problem_id=source_problem_id,
                    output_path=str(output_path.relative_to(self.output_dir)),
                    reason=exc.reason,
                    attempts=exc.attempts,
                    status_code=exc.status_code,
                )
            )
            print(f"[error] image request failed for {url}: {exc.reason}", file=sys.stderr, flush=True)

    def _extract_metadata(self, soup: BeautifulSoup) -> dict[str, str]:
        metadata: dict[str, str] = {}
        info_table = soup.select_one("#problem-info")
        if not info_table:
            return metadata

        cells = [collapse_whitespace(cell.get_text(" ", strip=True)) for cell in info_table.select("td")]
        keys = [
            "time_limit",
            "memory_limit",
            "submissions",
            "accepted",
            "solved_users",
            "accept_rate",
        ]
        for key, value in zip(keys, cells):
            metadata[key] = value
        return metadata

    def _extract_tags(self, soup: BeautifulSoup) -> list[str]:
        tags = []
        for tag in soup.select('[href^="/problemset?sort=no_asc&algo="], [href^="/problem/tag/"]'):
            text = collapse_whitespace(tag.get_text(" ", strip=True))
            if text:
                tags.append(text)
        seen = set()
        deduped = []
        for tag in tags:
            if tag in seen:
                continue
            seen.add(tag)
            deduped.append(tag)
        return deduped

    def _extract_samples(self, soup: BeautifulSoup) -> list[tuple[str, str]]:
        samples: list[tuple[str, str]] = []
        for section in soup.select(
            '[id^="sampleinput"], [id^="sampleoutput"]'
        ):
            heading = text_or_none(section.select_one("h2"))
            pre = section.select_one("pre")
            if not heading or not pre:
                continue
            heading = re.sub(r"\s+복사$", "", heading).strip()
            content = pre.get_text("\n", strip=False).strip("\n")
            samples.append((heading, f"```text\n{content}\n```"))
        return samples

    def _download_embedded_assets(self, section: Tag, asset_dir: Path, problem_id: int) -> None:
        for img_index, img in enumerate(section.select("img"), start=1):
            src = img.get("src")
            if not src:
                continue
            absolute_url = urljoin(BASE_URL, src)
            filename = build_asset_filename(absolute_url, img_index)
            local_path = asset_dir / filename
            if not local_path.exists():
                try:
                    self._download_binary(absolute_url, local_path)
                except RequestFailedError as exc:
                    self.log_failure(
                        FailedRequestRecord(
                            resource_type="image",
                            url=absolute_url,
                            problem_id=problem_id,
                            source_problem_id=problem_id,
                            output_path=str(local_path.relative_to(self.output_dir)),
                            reason=exc.reason,
                            attempts=exc.attempts,
                            status_code=exc.status_code,
                            selector=section.get("id"),
                        )
                    )
                    print(
                        f"[error] image request failed for problem {problem_id}: {absolute_url}",
                        file=sys.stderr,
                        flush=True,
                    )
            relative_path = Path("..") / "assets" / str(problem_id) / filename
            img["src"] = relative_path.as_posix()

    def _download_binary(self, url: str, output_path: Path) -> None:
        response = self.request(url, stream=True)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("wb") as fp:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    fp.write(chunk)

    def _html_to_markdown(self, section: Tag) -> str:
        html = str(section)
        converted = md(
            html,
            heading_style="ATX",
            bullets="-",
            strong_em_symbol="*",
        ).strip()

        # markdownify leaves the wrapper heading in place; top-level section titles
        # are already handled separately.
        converted = re.sub(r"^##\s+.+?\n+", "", converted, count=1, flags=re.MULTILINE)
        return converted.strip()

    def _render_markdown(
        self,
        *,
        problem_id: int,
        title: str,
        sections: list[tuple[str, str]],
    ) -> str:
        lines = [f"# {problem_id}번: {title}", ""]

        for label, content in sections:
            if not content:
                continue
            lines.append(f"## {label}")
            lines.append("")
            lines.append(content.rstrip())
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"


def collapse_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def text_or_none(tag: Tag | None) -> str | None:
    if tag is None:
        return None
    text = collapse_whitespace(tag.get_text(" ", strip=True))
    return text or None


def guess_extension(url: str) -> str:
    path = urlparse(url).path
    suffix = Path(path).suffix
    if suffix and len(suffix) <= 8:
        return suffix
    return ""


def build_asset_filename(url: str, img_index: int) -> str:
    suffix = guess_extension(url) or ".bin"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return f"image-{img_index}-{digest}{suffix}"


def parse_optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_problem_id(url: str, fallback: object) -> int | None:
    parsed = parse_optional_int(fallback)
    if parsed is not None:
        return parsed
    match = re.search(r"/problem/(\d+)", url)
    if match:
        return int(match.group(1))
    return None


def parse_only_ids(raw_ids: str | None) -> list[int] | None:
    if not raw_ids:
        return None
    values = []
    for token in raw_ids.split(","):
        token = token.strip()
        if not token:
            continue
        if not token.isdigit():
            raise argparse.ArgumentTypeError(f"invalid problem id: {token}")
        values.append(int(token))
    return values or None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Crawl Baekjoon problems and save each problem as Markdown.",
    )
    parser.add_argument(
        "--output-dir",
        default="archive",
        help="directory where markdown files and assets will be stored",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.7,
        help="delay between requests in seconds",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="per-request timeout in seconds",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="number of retries for failed requests",
    )
    parser.add_argument(
        "--start-id",
        type=int,
        default=1000,
        help="first problem id to crawl",
    )
    parser.add_argument(
        "--end-id",
        type=int,
        default=None,
        help="last problem id to crawl",
    )
    parser.add_argument(
        "--only-ids",
        type=parse_only_ids,
        default=None,
        help="comma-separated explicit problem ids to crawl",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="HTTP user agent",
    )
    parser.add_argument(
        "--save-html",
        action="store_true",
        help="also save raw HTML pages",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="number of worker threads for crawling problem pages",
    )
    parser.add_argument(
        "--retry-log-input",
        type=Path,
        default=None,
        help="retry only the failed requests listed in this JSONL log file",
    )
    parser.add_argument(
        "--failure-log-path",
        type=Path,
        default=None,
        help="path to write failed request JSONL records",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = CrawlConfig(
        output_dir=Path(args.output_dir),
        delay=args.delay,
        timeout=args.timeout,
        retries=args.retries,
        start_id=args.start_id,
        end_id=args.end_id,
        user_agent=args.user_agent,
        save_html=args.save_html,
        only_ids=args.only_ids,
        workers=max(1, args.workers),
        retry_log_input=args.retry_log_input,
        failure_log_path=args.failure_log_path,
    )
    crawler = BaekjoonCrawler(config)
    crawler.crawl_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
