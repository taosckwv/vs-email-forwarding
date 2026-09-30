#!/usr/bin/env python3
"""交付前归档自检：只读 tar，判断镜像归档是不是「单平台 linux/amd64」。

为什么需要这一步（背景事实）：
  * 服务器实测 CLI 为 Docker 26.1.3（2026-09）；`docker save` / `docker load`
    的 `--platform` 参数要 CLI v28+，服务器上根本传不了这个参数，
    所以交付链改成「构建期钉死平台 + 保存前/载入后校验架构」；
  * Engine 26 默认使用 **classic image store**，而 classic image store
    **不支持 image index / manifest list**：归档里一旦带了 manifest list 或
    attestation（这是 containerd image store 导出时才会出现的东西），
    服务器 `docker load` 就会失败；
  * 开发机用 Docker Desktop，很可能是 containerd image store，所以
    「开发机存出来的归档到底是不是单平台」必须在交付前查一次。

为什么不用 docker 命令查：开发机上可能没有 Docker（本项目当前就是如此），
而 image store 模式的差异恰恰是「用 docker 命令在本机看不出来」的那部分。
本脚本只用 Python 标准库（json + tarfile）读归档，任何一台装了 Python 的机器
都能跑，不联网。

用法：
    py -3.13 deploy/check-image-archive.py dist/vs-email-forwarding-images-0.1.0.tar
    python3 deploy/check-image-archive.py dist/vs-email-forwarding-images-0.1.0.tar --expected-images 4

判读标准（详见 deploy/README.md）：
  通过 = 归档里没有 image index / manifest list 条目、没有 attestation 条目、
         每个条目的平台都是 linux/amd64、条目数不超过本次打包的镜像个数；
  失败 = 出现上面任意一种情况 —— 归档很可能来自 containerd image store 的
         多平台导出，classic image store 的 `docker load` 载不动。

退出码：
    0 = 通过（单平台归档）
    1 = 归档不合格（多平台 / 带 attestation / 平台不是 linux/amd64）
    2 = 用法或读文件出错
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile

TARGET_PLATFORM = "linux/amd64"

MEDIA_INDEX_OCI = "application/vnd.oci.image.index.v1+json"
MEDIA_LIST_DOCKER = "application/vnd.docker.distribution.manifest.list.v2+json"

INDEX_MEDIA_TYPES = (MEDIA_INDEX_OCI, MEDIA_LIST_DOCKER)

ATTEST_ANNOTATION = "vnd.docker.reference.type"
ATTEST_VALUE = "attestation-manifest"

REF_NAME_KEYS = (
    "org.opencontainers.image.ref.name",
    "io.containerd.image.name",
)


def log(message: str = "") -> None:
    print(message)


def fail(message: str, code: int = 1) -> None:
    print("[失败] " + message, file=sys.stderr)
    raise SystemExit(code)


def blob_path(digest: str) -> str:
    """OCI 布局里 blob 的路径：sha256:abcd... -> blobs/sha256/abcd..."""
    if digest.startswith("sha256:"):
        return "blobs/sha256/" + digest.split(":", 1)[1]
    return "blobs/sha256/" + digest


class Archive:
    """只读访问一个未压缩的镜像 tar（不依赖 Docker、不依赖第三方库）。

    tar 句柄由调用方用 with 打开并关闭（见 main），这里只做只读访问。
    """

    def __init__(self, tar: tarfile.TarFile) -> None:
        self._tar = tar
        self._members: dict = {}
        for member in tar.getmembers():
            self._members[member.name.lstrip("./")] = member
        self.names = sorted(self._members)

    def read(self, name: str):
        member = self._members.get(name.lstrip("./"))
        if member is None:
            return None
        try:
            handle = self._tar.extractfile(member)
        except tarfile.TarError:
            return None
        if handle is None:
            return None
        return handle.read()

    def read_json(self, name: str):
        raw = self.read(name)
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            fail("归档内的 " + name + " 无法解析：" + str(exc), 1)
            return None


def config_platform(archive: Archive, digest: str):
    """按 manifest blob 里的 config.digest 读配置，返回 "os/arch"（读不到返回空串）。"""
    manifest = archive.read_json(blob_path(digest))
    if not isinstance(manifest, dict):
        return ""
    config = manifest.get("config")
    if not isinstance(config, dict) or not config.get("digest"):
        return ""
    cfg = archive.read_json(blob_path(str(config["digest"])))
    if not isinstance(cfg, dict):
        return ""
    os_name = str(cfg.get("os", "") or "")
    arch = str(cfg.get("architecture", "") or "")
    if os_name and arch:
        return os_name + "/" + arch
    return ""


def describe_oci_entry(archive: Archive, entry: dict):
    """返回 (平台, 问题说明, 引用清单)。"""
    media = str(entry.get("mediaType", "") or "")
    annotations = entry.get("annotations") or {}
    platform = entry.get("platform") or {}
    digest = str(entry.get("digest", "") or "")

    refs = []
    for key in REF_NAME_KEYS:
        value = annotations.get(key)
        if value:
            refs.append(str(value))

    if media in INDEX_MEDIA_TYPES:
        label = media or "mediaType 缺失"
        return "", "这一条本身就是一个 image index / manifest list（" + label + "）", refs

    if annotations.get(ATTEST_ANNOTATION) == ATTEST_VALUE:
        return (
            "",
            "这一条是 attestation（" + ATTEST_ANNOTATION + "=" + ATTEST_VALUE + "）",
            refs,
        )

    index_platform = ""
    os_name = str(platform.get("os", "") or "")
    arch = str(platform.get("architecture", "") or "")
    if os_name and arch:
        index_platform = os_name + "/" + arch

    config_p = config_platform(archive, digest) if digest else ""

    if index_platform and config_p and index_platform != config_p:
        return (
            config_p,
            "index 里写的 platform（" + index_platform + "）与配置 blob（" + config_p + "）不一致",
            refs,
        )
    if index_platform:
        return index_platform, "", refs
    if config_p:
        return config_p, "index 里没有 platform 字段，平台取自配置 blob", refs
    return "", "既没有 platform 字段，也读不到配置 blob，无法确认平台", refs


def collect(archive: Archive):
    """返回 (形态说明, [(平台, 问题, 引用)], 顶层问题列表)。"""
    index = archive.read_json("index.json")
    manifest = archive.read_json("manifest.json")

    problems: list = []
    rows: list = []

    if isinstance(index, dict):
        entries = index.get("manifests")
        if not isinstance(entries, list):
            fail("index.json 里没有 manifests 数组，归档格式不符合预期。", 1)
        for entry in entries:
            if not isinstance(entry, dict):
                problems.append("index.json 里有不是 JSON 对象的条目")
                continue
            rows.append(describe_oci_entry(archive, entry))
        return "OCI 布局（index.json）", rows, problems

    if isinstance(manifest, list):
        for entry in manifest:
            if not isinstance(entry, dict):
                problems.append("manifest.json 里有不是 JSON 对象的条目")
                continue
            refs = [str(tag) for tag in (entry.get("RepoTags") or [])]
            config_path = str(entry.get("Config", "") or "")
            cfg = archive.read_json(config_path) if config_path else None
            platform = ""
            if isinstance(cfg, dict):
                os_name = str(cfg.get("os", "") or "")
                arch = str(cfg.get("architecture", "") or "")
                if os_name and arch:
                    platform = os_name + "/" + arch
            problem = ""
            if not platform:
                problem = (
                    "读不到配置 blob（" + (config_path or "Config 字段缺失") + "），无法确认平台"
                )
            rows.append((platform, problem, refs))
        return "docker-archive 布局（manifest.json，legacy 形态，天然单平台）", rows, problems

    fail(
        "归档根目录下既没有 index.json 也没有 manifest.json，这不是 docker save 的产物"
        "（也许是 tar.gz、或用了别的导出方式）。",
        1,
    )
    return "", [], []


def print_remediation() -> None:
    log("这通常意味着：开发机的 Docker 用的是 containerd image store（或在做多平台导出），")
    log("docker save 导出的是带 manifest list / attestation 的 image index，")
    log("而服务器的 classic image store 不支持 image index，docker load 会失败。")
    log("")
    log("处置方向（本机未装 Docker，以下路径未在本机实测，需在开发机上逐条确认；")
    log("改完必须重跑本脚本并重新生成 sha256）：")
    log("  1) 开发机 Docker Desktop 关掉 containerd image store")
    log("     （Settings -> General -> Use containerd for pulling and storing images），")
    log("     重新 docker save，通常会得到 legacy docker-archive 形态（manifest.json）；")
    log("  2) 开发机 CLI 若是 v28+（Docker Desktop 常见），可用")
    log("     docker save --platform linux/amd64 只导出单平台")
    log("     （注意：服务器 CLI 26.1.3 用不了这个参数，但这一步在开发机上执行）；")
    log("  3) 也可以用 docker buildx build --platform linux/amd64 --output type=docker")
    log("     直接产出单平台 docker-archive。")


def main(argv) -> int:
    parser = argparse.ArgumentParser(
        description="交付前归档自检：确认镜像 tar 是单平台 linux/amd64（不依赖 Docker）。",
    )
    parser.add_argument("archive", help="docker save 产出的镜像 tar（未压缩）")
    parser.add_argument(
        "--expected-images",
        type=int,
        default=None,
        help="本次打包的镜像个数（默认 3：应用镜像两个 tag + postgres:17；带 -IncludeRehearsal 时为 4）",
    )
    parser.add_argument(
        "--include-rehearsal",
        action="store_true",
        help="按 4 个镜像计算（save-image.ps1 -IncludeRehearsal 会多打一个 almalinux:8）",
    )
    args = parser.parse_args(argv)

    expected = args.expected_images
    if expected is None:
        expected = 4 if args.include_rehearsal else 3
    if expected < 1:
        fail("--expected-images 必须是大于等于 1 的整数。", 2)

    log("==> 交付前归档自检（不依赖 Docker，只读 tar 内的 index.json / manifest.json）")
    log("    归档          ：" + args.archive)
    log("    目标平台      ：" + TARGET_PLATFORM)
    log("    本次镜像个数  ：" + str(expected))
    log("")

    layout = ""
    rows: list = []
    problems: list = []
    try:
        with tarfile.open(args.archive, "r:") as tar:
            layout, rows, problems = collect(Archive(tar))
    except (OSError, tarfile.TarError) as exc:
        fail("打不开或读不了归档 " + args.archive + "：" + str(exc), 2)
    if not layout:
        fail("没能读出归档结构，无法判断是不是单平台归档。", 1)

    log("    归档形态      ：" + layout)
    platforms = []
    refs_all = []
    for position, (platform, problem, refs) in enumerate(rows):
        if platform:
            platforms.append(platform)
        refs_all.extend(refs)
        line = "      [" + str(position) + "] " + (platform or "平台未知")
        if refs:
            line += "  引用=" + ", ".join(refs)
        log(line)
        if problem:
            problems.append(problem)

    log("")
    log("    平台分布      ：" + (", ".join(sorted(set(platforms))) if platforms else "无"))
    log("    镜像条目数    ：" + str(len(rows)) + "（本次打包 " + str(expected) + " 个）")
    if refs_all:
        log("    引用          ：" + ", ".join(sorted(set(refs_all))))
    log("")

    if not rows:
        problems.append("归档里没有任何可识别的镜像 manifest 条目")

    for platform in sorted(set(platforms)):
        if platform != TARGET_PLATFORM:
            problems.append(
                "条目平台是 "
                + platform
                + "，不是 "
                + TARGET_PLATFORM
                + "（classic image store 只能装单平台，架构不一致时服务器上会 exec format error）"
            )

    if len(rows) > expected:
        problems.append(
            "镜像条目数 "
            + str(len(rows))
            + " 多于本次打包的镜像个数 "
            + str(expected)
            + "：归档里很可能带了 image index / manifest list 或 attestation"
        )
    elif len(rows) < expected and not problems:
        log(
            "[警告] 镜像条目数 "
            + str(len(rows))
            + " 少于本次打包的镜像个数 "
            + str(expected)
            + "：请确认 tar 里没有漏镜像（deploy/load-image.sh 会逐个校验 tag 是否存在）"
        )

    if problems:
        log("")
        log("[失败] 归档自检不通过：")
        for problem in problems:
            log("  - " + problem)
        log("")
        print_remediation()
        return 1

    log("[通过] 归档是单平台 " + TARGET_PLATFORM + " 归档：没有 image index / manifest list，")
    log("       没有 attestation，平台一致，条目数不超过本次打包的镜像个数。")
    log("       可以交付；服务器上按 deploy/load-image.sh 载入并做载入后校验。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
