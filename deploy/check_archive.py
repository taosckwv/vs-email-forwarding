#!/usr/bin/env python3
"""交付前归档自检：只读 tar，判断基础镜像归档是不是「单平台 linux/amd64」。

为什么需要这一步（结论真源：docs/research/01-delivery-and-deploy.md 的「结论」段）：
  * 生产服务器实测 CLI / Engine 均为 26.1.3、**API 1.45**，而 `docker save` / `docker load`
    的 `--platform` 参数需要 **API 1.48（Engine v28）**——服务器上根本传不了这个参数，
    所以平台改由「构建期钉死 + 归档进服务器之前自检 + 载入之后逐 tag 复检」保证；
  * Engine 26 默认 **classic image store**，它**载不动 image index / manifest list**：
    归档里一旦带了 manifest list 或 attestation，服务器 `docker load` 就会失败；
  * 本脚本**不依赖 Docker、只用 Python 标准库**，因此可以在产出方（哪台机器尚未定，
    D13）与服务器之外的任意一台装了 Python 的机器上执行，且不联网。

与相邻脚本的分工（**不要在本脚本里重复它们**）：
  * 本脚本 = 归档**进服务器之前**的结构自检。**必须在生成 `<tar>.sha256` 旁路文件之前执行**——
    自检不通过就不要生成旁路文件、更不要传上去；旁路文件一旦生成，它记的就是这份 tar 的 hash。
  * `deploy/run.sh` = 落地的**服务器侧**执行：`up -d` 前对应用镜像**与 compose 依赖的
    镜像（含基础镜像 postgres:17）**逐个复检存在性与架构（`docker image inspect`）、
    解析 `.env`，`up -d` 后轮询 `/healthz`；它不读 tar、也不做本自检。
  * **tar 的导出、带入与 `docker load` 流程本期不在范围**（属 D2-乙，随 D13 推迟；
    见 docs/open-questions.md）。所以本脚本不读旁路文件、不校验传输完整性——
    那是载入链路一侧的事。

判读标准：
  通过 = 两种布局都支持（OCI 的 `index.json` / legacy 的 `manifest.json`）；归档内没有
         image index / manifest list 条目、没有 attestation 条目、**每个条目都能判定出
         平台且都是 linux/amd64**（index 无 `platform` 时以配置 blob 的 `os/architecture`
         为准；两者都读不出即「平台不可判定」，判失败——**不核对任何架构不许放行**），
         且镜像条目数**恰好等于** `--expected-images`（默认 3）。
  失败 = 出现上面任意一种情况。条目数少了也判失败：归档本该装满本次要带的基础镜像，
         缺一个就是漏带，不能只当成警告。

退出码（语义固定，不要改）：
    0 = 通过
    1 = 归档不合格
    2 = 用法或读取错误

用法：
    python3 deploy/check_archive.py dist/base-images.tar
    python3 deploy/check_archive.py dist/base-images.tar --expected-images 3

⚠️ 本脚本**不内嵌任何基础镜像 tag 的 digest**：digest 会随时间漂移，册里记的观测值
   一律不是现值，写进脚本就等于埋一个必然过期且无人复核的常量。需要核对 digest 时，
   请按 docs/research/01-delivery-and-deploy.md 的要求在写进交付记录**之前重新复核**。
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
from dataclasses import dataclass, field
from typing import Any

#: 目标平台：classic image store 只能装单平台，且生产宿主是 amd64。
#: 唯一真正钉死架构的环节是构建期 `docker build --platform linux/amd64`；
#: 本自检只做「确认」，不会把镜像变成 amd64。
TARGET_PLATFORM = "linux/amd64"

#: 本次交付必须装满的基础镜像个数：python（构建期）+ uv（构建期）+ postgres:17（运行期）。
EXPECTED_IMAGES = 3

#: 退出码语义固定（见模块 docstring）。
EXIT_OK = 0
EXIT_BAD_ARCHIVE = 1
EXIT_USAGE_OR_READ_ERROR = 2

MEDIA_INDEX_OCI = "application/vnd.oci.image.index.v1+json"
MEDIA_LIST_DOCKER = "application/vnd.docker.distribution.manifest.list.v2+json"
INDEX_MEDIA_TYPES = (MEDIA_INDEX_OCI, MEDIA_LIST_DOCKER)

#: attestation 条目在 OCI index 里的标记（构建期 provenance 导出的产物）。
ATTEST_ANNOTATION = "vnd.docker.reference.type"
ATTEST_VALUE = "attestation-manifest"


@dataclass(frozen=True)
class Finding:
    """一条自检发现。kind 决定它算不合格还是只提示。"""

    kind: str
    message: str


@dataclass
class Report:
    """一次自检的累积结果。"""

    layout: str = ""
    rows: list[tuple[str, list[str]]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def bad(self, message: str) -> None:
        """记一条不合格项。"""
        self.findings.append(Finding("不合格", message))

    def warn(self, message: str) -> None:
        """记一条只作提示的项。"""
        self.findings.append(Finding("提示", message))

    def add_row(self, platform: str, notes: list[str] | None = None) -> None:
        """记一条镜像条目（平台可能为空 = 读不出来）。"""
        self.rows.append((platform, list(notes or [])))

    def blocked(self) -> bool:
        """是否有不合格项。"""
        return any(finding.kind == "不合格" for finding in self.findings)


def log(message: str = "") -> None:
    """输出一行人类可读的进度或结论。"""
    print(message)


def fail(message: str, code: int = EXIT_BAD_ARCHIVE) -> None:
    """打印失败信息并以指定退出码结束（1 = 归档不合格，2 = 用法 / 读取错误）。"""
    print("[失败] " + message, file=sys.stderr)
    raise SystemExit(code)


def blob_path(digest: str) -> str:
    """OCI 布局里 blob 的路径：sha256:abcd... -> blobs/sha256/abcd..."""
    if digest.startswith("sha256:"):
        return "blobs/sha256/" + digest.split(":", 1)[1]
    return "blobs/sha256/" + digest


class Archive:
    """只读访问一个未压缩的镜像 tar（只用标准库，不依赖 Docker 与第三方包）。

    tar 句柄由调用方用 with 打开（见 main），本类只做只读访问。
    """

    def __init__(self, tar: tarfile.TarFile) -> None:
        self._tar = tar
        self._members: dict[str, tarfile.TarInfo] = {}
        for member in tar.getmembers():
            self._members[member.name.lstrip("./")] = member
        self.names = sorted(self._members)

    def read(self, name: str) -> bytes | None:
        """读一个成员的字节内容；成员不存在或读不了就返回 None。"""
        member = self._members.get(name.lstrip("./"))
        if member is None:
            return None
        try:
            handle = self._tar.extractfile(member)
        except tarfile.TarError:
            return None
        if handle is None:
            return None
        with handle:
            return handle.read()

    def read_json(self, name: str) -> Any:
        """读 JSON 成员；解析失败按「归档不合格」处理（不是读取错误）。"""
        raw = self.read(name)
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            fail("归档内的 " + name + " 不是合法 JSON：" + str(exc))
            return None


def config_entry(archive: Archive, digest: str) -> dict[str, Any] | None:
    """按 manifest blob 里的 config.digest 取出配置对象（读不到返回 None）。"""
    manifest = archive.read_json(blob_path(digest))
    if not isinstance(manifest, dict):
        return None
    config = manifest.get("config")
    if not isinstance(config, dict) or not config.get("digest"):
        return None
    payload = archive.read_json(blob_path(str(config["digest"])))
    return payload if isinstance(payload, dict) else None


def config_platform(archive: Archive, digest: str) -> str:
    """配置 blob 里写的 "os/arch"；读不到返回空串。"""
    payload = config_entry(archive, digest) if digest else None
    if payload is None:
        return ""
    os_name = str(payload.get("os", "") or "")
    arch = str(payload.get("architecture", "") or "")
    return os_name + "/" + arch if os_name and arch else ""


def parse_oci(archive: Archive, index: Any, report: Report) -> None:
    """按 OCI 布局（index.json）逐条判读。"""
    report.layout = "OCI 布局（index.json）"
    entries = index.get("manifests") if isinstance(index, dict) else None
    if not isinstance(entries, list):
        report.bad("index.json 里没有 manifests 数组，归档不符合 OCI 布局")
        return
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            report.bad("index.json 的第 " + str(position) + " 条不是 JSON 对象")
            continue
        judge_oci_entry(archive, entry, position, report)


def _judge_entry_kind(entry: dict[str, Any], position: int, report: Report) -> bool:
    """判读索引条目的「类型」这一族故障；判为不合格返回 True，由调用方直接收工。

    三种情况都属于「这个条目根本不该出现在单平台交付归档里」，必须先于平台判定处理：
    mediaType 缺失、条目本身是嵌套的 index / manifest list、条目是 attestation。
    """
    media = str(entry.get("mediaType", "") or "")
    if not media:
        report.add_row("", ["条目类型=mediaType 缺失"])
        report.bad(
            "第 "
            + str(position)
            + " 条没有 mediaType：无法判断它是镜像 manifest 还是 index/manifest list，"
            "不能静默放行（重新 `docker save --platform linux/amd64` 导出）"
        )
        return True

    if media in INDEX_MEDIA_TYPES:
        report.add_row("", ["条目类型=" + media])
        report.bad(
            "第 "
            + str(position)
            + " 条本身就是一个 image index / manifest list（"
            + media
            + "）：classic image store 载不动它"
        )
        return True

    annotations = entry.get("annotations") or {}
    if annotations.get(ATTEST_ANNOTATION) == ATTEST_VALUE:
        report.add_row("", ["条目类型=attestation"])
        report.bad(
            "第 "
            + str(position)
            + " 条是 attestation（"
            + ATTEST_ANNOTATION
            + "="
            + ATTEST_VALUE
            + "）：单平台归档不应带它"
        )
        return True
    return False


def judge_oci_entry(archive: Archive, entry: dict[str, Any], position: int, report: Report) -> None:
    """判读 OCI index 里的一条 manifest 条目（条目类型先由 _judge_entry_kind 处理）。"""
    platform = entry.get("platform") or {}
    digest = str(entry.get("digest", "") or "")
    notes: list[str] = []

    if _judge_entry_kind(entry, position, report):
        return

    index_platform = platform_string(platform)
    config_p = config_platform(archive, digest)
    if index_platform and config_p and index_platform != config_p:
        report.add_row(config_p, [*notes, "index 声明=" + index_platform])
        report.bad(
            "第 "
            + str(position)
            + " 条：index 里的平台（"
            + index_platform
            + "）与配置 blob（"
            + config_p
            + "）不一致"
        )
        return
    if not index_platform and not config_p:
        report.add_row("", [*notes, "index 与配置 blob 都读不出平台"])
        report.bad(
            "第 "
            + str(position)
            + " 条平台不可判定（index 无 platform 且 config blob 读不出/缺 os·architecture）："
            "真 `docker save` 的 index 条目本来就没有 platform 字段，平台完全依赖 config blob，"
            "读不出时无法证明它是 " + TARGET_PLATFORM + "——不能当作通过"
        )
        return
    if not index_platform:
        notes.append("index 里没有 platform 字段")
    report.add_row(config_p or index_platform, notes)


def parse_legacy(archive: Archive, manifest: Any, report: Report) -> None:
    """按 legacy docker-archive 布局（manifest.json）逐条判读。"""
    report.layout = "docker-archive 布局（manifest.json，legacy 形态：一个条目 = 一个单平台镜像）"
    if not isinstance(manifest, list):
        report.bad("manifest.json 不是数组，归档不符合 docker-archive 布局")
        return
    for position, entry in enumerate(manifest):
        if not isinstance(entry, dict):
            report.bad("manifest.json 的第 " + str(position) + " 条不是 JSON 对象")
            continue
        config_path = str(entry.get("Config", "") or "")
        payload = archive.read_json(config_path) if config_path else None
        platform = ""
        if isinstance(payload, dict):
            os_name = str(payload.get("os", "") or "")
            arch = str(payload.get("architecture", "") or "")
            if os_name and arch:
                platform = os_name + "/" + arch
        tags = [str(tag) for tag in (entry.get("RepoTags") or [])]
        notes = (["tag=" + ", ".join(tags)] if tags else []) + ["配置=" + config_path]
        report.add_row(platform, notes)
        if not platform:
            report.bad(
                "第 "
                + str(position)
                + " 条读不出平台（配置 blob："
                + (config_path or "Config 字段缺失")
                + "）"
            )


def platform_string(platform: Any) -> str:
    """把 index 条目里的 platform 对象拼成 "os/arch"（缺失时返回空串）。"""
    if not isinstance(platform, dict):
        return ""
    os_name = str(platform.get("os", "") or "")
    arch = str(platform.get("architecture", "") or "")
    return os_name + "/" + arch if os_name and arch else ""


def collect(archive: Archive) -> Report:
    """按归档布局分派判读，返回累积结果。"""
    report = Report()
    index = archive.read_json("index.json")
    manifest = archive.read_json("manifest.json")
    if index is not None:
        parse_oci(archive, index, report)
        return report
    if manifest is not None:
        parse_legacy(archive, manifest, report)
        return report
    fail(
        "归档根目录下既没有 index.json 也没有 manifest.json：这不是 docker save 的产物"
        "（也许是 tar.gz、或用了别的导出方式）"
    )
    return report


def print_rows(report: Report) -> None:
    """打印逐条条目与平台分布。"""
    for position, (platform, notes) in enumerate(report.rows):
        line = "      [" + str(position) + "] " + (platform or "平台未知")
        if notes:
            line += "  " + "  ".join(notes)
        log(line)
    shown = ", ".join(sorted({platform for platform, _ in report.rows if platform}))
    log("    平台分布      ：" + (shown or "无"))
    log("    镜像条目数    ：" + str(len(report.rows)))


def judge_totals(report: Report, expected: int) -> None:
    """判读平台集合与镜像条目数（条目数必须恰好等于期望值）。"""
    if not report.rows:
        report.bad("归档里没有任何可识别的镜像 manifest 条目")
        return
    for platform in sorted({platform for platform, _ in report.rows if platform}):
        if platform != TARGET_PLATFORM:
            report.bad(
                "条目平台是 "
                + platform
                + "，不是 "
                + TARGET_PLATFORM
                + "（classic image store 只能装单平台；架构不符时服务器上会 exec format error）"
            )
    count = len(report.rows)
    if count > expected:
        report.bad(
            "镜像条目数 "
            + str(count)
            + " 多于本次交付的 "
            + str(expected)
            + " 个基础镜像：归档里很可能带了 image index / manifest list 或 attestation"
        )
    elif count < expected:
        report.bad(
            "镜像条目数 "
            + str(count)
            + " 少于本次交付的 "
            + str(expected)
            + " 个基础镜像：归档里漏了镜像，传上去也没法补"
        )


def print_outcome(report: Report) -> int:
    """打印结论并返回退出码。"""
    if report.blocked():
        log("")
        log("[失败] 归档自检不通过：")
        for finding in report.findings:
            if finding.kind == "不合格":
                log("  - " + finding.message)
        log("")
        log(
            "处置方向：回产出方重新导出**单平台**归档（构建期 `docker build --platform linux/amd64`；"
        )
        log(
            "          `docker save` / `docker load` 一律**不传** `--platform`——那是 API 1.48 的参数，"
        )
        log("          服务器实测 API 1.45 传不了）。**不要**把这份 tar 传给服务器，也不要为它生成")
        log("          `<tar>.sha256` 旁路文件——本自检必须在生成旁路文件之前执行。")
        return EXIT_BAD_ARCHIVE
    for finding in report.findings:
        if finding.kind == "提示":
            log("    [提示] " + finding.message)
    verified = sorted({platform for platform, _ in report.rows if platform})
    if verified:
        platform_sentence = "平台一致（逐条核对：" + ", ".join(verified) + "）、"
    else:
        platform_sentence = "平台逐条未核对（各条目平台均不可判读）、"
    log(
        "[通过] 归档是单平台 "
        + TARGET_PLATFORM
        + " 归档：无 image index / manifest list、无 attestation、"
    )
    log(
        "       "
        + platform_sentence
        + "镜像条目数与本次交付一致。下一步才生成 `<tar>.sha256` 旁路文件"
    )
    log('       （格式固定 `"<hash>  <name>"` 两个空格 + LF），再交给介质传输。')
    log(
        "       载入之后还必须逐 tag `inspect` 复检架构（见 docs/research/01-delivery-and-deploy.md）。"
    )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器；`--help` 就是本脚本的用法说明。"""
    parser = argparse.ArgumentParser(
        prog="check_archive.py",
        description=(
            "交付前归档自检：确认基础镜像 tar 是单平台 linux/amd64"
            "（不依赖 Docker，只用 Python 标准库）。"
        ),
        epilog=(
            "退出码：0 = 通过；1 = 归档不合格；2 = 用法或读取错误。"
            "本自检必须在生成 `<tar>.sha256` 旁路文件之前执行。"
        ),
    )
    parser.add_argument("archive", help="docker save 产出的镜像 tar（未压缩）")
    parser.add_argument(
        "--expected-images",
        type=int,
        default=EXPECTED_IMAGES,
        metavar="N",
        help=(
            "本次交付的基础镜像个数（默认 "
            + str(EXPECTED_IMAGES)
            + "：python 基础层 + uv 构建层 + postgres:17 运行期）。"
            "条目数多于或少于它都判不合格。"
        ),
    )
    return parser


def main(argv: list[str]) -> int:
    """入口：解析参数、自检、按固定退出码返回。"""
    args = build_parser().parse_args(argv)
    if args.expected_images < 1:
        fail("--expected-images 必须是大于等于 1 的整数", EXIT_USAGE_OR_READ_ERROR)

    log("==> 交付前归档自检（不依赖 Docker；只读 tar 内的 index.json / manifest.json）")
    log("    归档          ：" + args.archive)
    log("    目标平台      ：" + TARGET_PLATFORM)
    log("    期望镜像个数  ：" + str(args.expected_images))
    log("")

    try:
        with tarfile.open(args.archive, "r:") as tar:
            report = collect(Archive(tar))
    except FileNotFoundError as exc:
        fail("打不开归档：" + str(exc), EXIT_USAGE_OR_READ_ERROR)
        return EXIT_USAGE_OR_READ_ERROR
    except (OSError, tarfile.TarError) as exc:
        fail("读不了归档 " + args.archive + "：" + str(exc), EXIT_USAGE_OR_READ_ERROR)
        return EXIT_USAGE_OR_READ_ERROR

    log("    归档形态      ：" + (report.layout or "未能判定"))
    print_rows(report)
    judge_totals(report, args.expected_images)
    log("")
    return print_outcome(report)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
