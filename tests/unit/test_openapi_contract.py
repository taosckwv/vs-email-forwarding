#!/usr/bin/env python3
"""契约不漂移：`create_app().openapi()` 与 `docs/api-contract.yaml` 逐字段比对（spec 0001 / T-38）。

两份产物互为校验对象（`plan.md` §7.3 的 `tests/unit/test_openapi_contract.py` 一行、§8.1 的
「契约两处漂移」风险）：

* `docs/api-contract.yaml` 是**对外接口契约的唯一真源**（`design.md` §4.1）——路径、字段名、
  必填、取值枚举、取值约束只写在它那里；
* FastAPI 从 `schemas/` 与 `api/v1/` 生成的 OpenAPI 是**实现实际对外提供的东西**。

比对的四个面按 `plan.md` 的指定口径：**路径（含方法与响应码）、字段名、必填、`enum`**，
另加契约里写明的参数取值约束（`limit.minimum`、`after.pattern`）与「未知字段不报错」
（`additionalProperties`）。期望值取自契约文件本身，**不取自被测代码**——本文件不 import
`schemas/`、不读 `CURSOR_PATTERN`，避免「拿实现校验实现」。

不比对的两类差异（各有归属，不是漂移）：

* 文档级元信息（`info.title` / `version` / `description`）：契约是给外部看的说明，实现侧标题
  是服务名，两者本就不必逐字相同；
* `Timestamp` 的 `format: date-time` 注解：契约声明的是**线上格式**（UTC RFC 3339 六位小数 +
  `Z`），由序列化器兑现，由 `tests/unit/test_schemas.py` 与集成用例
  `tests/integration/test_api_contract.py` 的取值比对判定；生成式 schema 只给出 `type: string`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import yaml

from vs_email_forwarding.main import create_app


def _repo_root() -> Path:
    """定位真仓库根：从本文件逐级向上找 ``docs/api-contract.yaml``。

    mutmut 把源树拷进 ``mutants/`` 沙箱再跑同一套单测，沙箱里只有 pyproject.toml /
    src / tests / uv.lock、**没有 docs/**——按固定层级（``parents[2]``）取根会在沙箱里
    指向不存在的路径（`T-41` 首次跑门禁即报 ``FileNotFoundError:
    mutants/docs/api-contract.yaml``）。逐级向上找则两种语境都成立：常规运行时**第三个候选**
    （``parents[2]``，即真仓库根）就命中；沙箱里同一个锚多走一级（``parents[3]``）同样落回真根，
    断言因此读的是**未变异**的真契约文件（与被变异的 ``schemas/`` 比对，判定力不受影响）。
    找不到即说明测试脱离了仓库检出，直接失败而不是静默跳过。

    本函数**有意自包含**、不抽公共 helper：三处同名函数的取根需求不同——两处锤
    ``docs/api-contract.yaml``（本文件与 `tests/unit/test_constants_consistency.py`）、
    一处锤 ``docs/security.md``（`tests/unit/test_review_placeholder.py`），而
    `tests/unit/test_constants_consistency.py` 还另需 `migrations/`；三处都为在 mutmut
    沙箱内**零导入耦合**而各自实现（只用标准库 `pathlib`），这是沙箱里唯一稳的取根方式
    （先例与起因见 `tests/unit/test_review_placeholder.py` 的同名函数）。
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "docs" / "api-contract.yaml").is_file():
            return candidate
    raise AssertionError("未找到仓库根：docs/api-contract.yaml 不在本文件的任何上级目录")


_REPO_ROOT: Final = _repo_root()
_CONTRACT_PATH: Final = _REPO_ROOT / "docs" / "api-contract.yaml"

#: 契约里**带 properties** 的 schema：字段名与必填逐项比对。
_FIELD_BEARING_SCHEMAS: Final = (
    "SubmitMessageRequest",
    "SubmitMessageResponse",
    "MessageStatusResponse",
    "RepliesPageResponse",
    "Reply",
    "HealthResponse",
)

#: 契约里把「ID」与「时刻」抽成了独立 schema，生成的文档把它们内联在字段上：
#: 这里比对**使用处**的形状（`id` 一律 uuid，时刻一律字符串）。
_INLINED_SCHEMAS: Final = {
    "MessageId": {"type": "string", "format": "uuid"},
    "Timestamp": {"type": "string"},
}

#: 契约声明的两个纯枚举 schema（生成的文档把它们内联在用到它们的字段上）。
_ENUM_SCHEMAS: Final = ("MessageState", "BodyFormat")


def _contract() -> dict[str, Any]:
    """读契约真源；YAML 或结构不对时在这里先红，不让下面的断言悄悄跳过。"""
    document = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict) and document.get("paths"), "契约文件没有 paths"
    assert document.get("components", {}).get("schemas"), "契约文件没有 components.schemas"
    return document


def _generated() -> dict[str, Any]:
    """应用实际生成的 OpenAPI（纯构造，不起服务、不连库）。"""
    document = create_app().openapi()
    assert document.get("paths"), "生成的 OpenAPI 没有 paths"
    return document


def _walk_enum_sets(node: Any) -> set[frozenset[str]]:
    """递归收集一个 schema 节点里出现的全部 `enum` 取值集合（`anyOf` / `allOf` 一并下去）。"""
    found: set[frozenset[str]] = set()
    if isinstance(node, dict):
        values = node.get("enum")
        if isinstance(values, list) and values:
            found.add(frozenset(str(value) for value in values))
        for value in node.values():
            found |= _walk_enum_sets(value)
    elif isinstance(node, list):
        for item in node:
            found |= _walk_enum_sets(item)
    return found


def _resolve_contract_ref(node: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    """把契约节点里的 `$ref` / `allOf` / `oneOf` / `anyOf` 展开成「包括各分支」的节点集合。"""
    branches: list[dict[str, Any]] = [node]
    queue = [node]
    while queue:
        current = queue.pop()
        ref = current.get("$ref")
        if isinstance(ref, str):
            name = ref.rsplit("/", 1)[-1]
            target = contract["components"]["schemas"][name]
            branches.append(target)
            queue.append(target)
        for key in ("allOf", "oneOf", "anyOf"):
            for child in current.get(key, []) or []:
                if isinstance(child, dict):
                    branches.append(child)
                    queue.append(child)
    return {"branches": branches}


def _declared_response_codes(operation: dict[str, Any]) -> set[str]:
    return {str(code) for code in operation.get("responses", {})}


def test_scan_faces_are_not_empty() -> None:
    """两份产物都必须真读到：任一份为空时，「不漂移」是空承诺。"""
    contract = _contract()
    generated = _generated()
    assert set(contract["paths"]) == {
        "/v1/messages",
        "/v1/messages/{id}",
        "/v1/messages/{id}/replies",
        "/healthz",
    }
    assert set(generated["components"]["schemas"]) >= set(_FIELD_BEARING_SCHEMAS)


def test_paths_and_methods_match() -> None:
    """路径与 HTTP 方法两份文档逐项一致（多一个少一个都算漂移）。"""
    contract = _contract()["paths"]
    generated = _generated()["paths"]
    assert set(generated) == set(contract)
    for path, operations in contract.items():
        assert set(generated[path]) == set(operations), f"{path} 的方法集合不一致"


def test_declared_response_codes_match() -> None:
    """每个操作的已声明响应码一致（契约只声明实际会出现的码，不声明「可能的错误」）。"""
    contract = _contract()["paths"]
    generated = _generated()["paths"]
    for path, operations in contract.items():
        for method, operation in operations.items():
            expected = _declared_response_codes(operation)
            actual = _declared_response_codes(generated[path][method])
            assert actual == expected, (
                f"{method.upper()} {path} 响应码不一致：{actual} != {expected}"
            )


def test_field_names_and_required_match() -> None:
    """带字段的 schema：属性名集合与 `required` 清单两份文档逐项一致。"""
    contract = _contract()
    generated = _generated()["components"]["schemas"]
    for name in _FIELD_BEARING_SCHEMAS:
        expected = contract["components"]["schemas"][name]
        actual = generated[name]
        assert set(actual.get("properties", {})) == set(expected["properties"]), (
            f"{name} 的字段名不一致"
        )
        assert set(actual.get("required", [])) == set(expected.get("required", [])), (
            f"{name} 的必填清单不一致"
        )


def test_enum_values_match_the_contract() -> None:
    """每个带 `enum` 的字段：生成的取值集合 == 契约里该字段（展开 `$ref` 后）的取值集合。"""
    contract = _contract()
    generated = _generated()["components"]["schemas"]
    checked = 0
    for name in _FIELD_BEARING_SCHEMAS:
        expected_props = contract["components"]["schemas"][name]["properties"]
        for prop, expected_node in expected_props.items():
            expected_sets = _walk_enum_sets(_resolve_contract_ref(expected_node, contract))
            if not expected_sets:
                continue
            actual_sets = _walk_enum_sets(generated[name]["properties"][prop])
            assert actual_sets == expected_sets, (
                f"{name}.{prop} 的 enum 不一致：{actual_sets} != {expected_sets}"
            )
            checked += 1
    assert checked == 4, (
        f"枚举面比预期少（应比对 4 处：请求的 body_format、两个响应的 status、回复的 body_format），"
        f"实为 {checked}"
    )


def test_enum_schemas_are_declared_with_the_same_values() -> None:
    """契约声明的两个纯枚举 schema 与生成文档里用到的取值集合一致，且顺序也一致。"""
    contract = _contract()
    for name in _ENUM_SCHEMAS:
        declared = [str(value) for value in contract["components"]["schemas"][name]["enum"]]
        assert declared, f"契约 schema {name} 没有 enum"
        # 生成文档把它们内联：把所有用到该枚举的字段收齐，取值集合必须与契约完全相同。
        used: set[frozenset[str]] = set()
        for schema_name in _FIELD_BEARING_SCHEMAS:
            for node in (
                _generated()["components"]["schemas"][schema_name].get("properties", {}).values()
            ):
                for values in _walk_enum_sets(node):
                    used.add(values)
        assert frozenset(declared) in used, f"{name} 的取值集合没有出现在生成文档里"


def test_inlined_schema_usages_match() -> None:
    """`MessageId` / `Timestamp` 的**使用处**形状与契约一致（契约把它们抽成了独立 schema）。"""
    contract = _contract()
    generated = _generated()["components"]["schemas"]
    # MessageId 的每个使用处都必须是 uuid 形状。
    id_usages = [
        (name, prop_name)
        for name in _FIELD_BEARING_SCHEMAS
        for prop_name, node in contract["components"]["schemas"][name]["properties"].items()
        if _is_ref(node, "MessageId")
    ]
    assert id_usages, "契约里没有任何字段引用 MessageId，扫描面为空"
    for name, prop_name in id_usages:
        actual = generated[name]["properties"][prop_name]
        assert actual.get("type") == _INLINED_SCHEMAS["MessageId"]["type"]
        assert actual.get("format") == _INLINED_SCHEMAS["MessageId"]["format"]
    # Timestamp 的每个使用处都必须是字符串（可空性由契约的 null 分支决定，逐项对上）。
    time_usages = [
        (name, prop_name, node)
        for name in _FIELD_BEARING_SCHEMAS
        for prop_name, node in contract["components"]["schemas"][name]["properties"].items()
        if _is_ref(node, "Timestamp")
    ]
    assert time_usages, "契约里没有任何字段引用 Timestamp，扫描面为空"
    for name, prop_name, node in time_usages:
        expected_types = {"string"} | _nullable_types(node)
        assert _walk_types(generated[name]["properties"][prop_name]) == expected_types


def test_parameter_constraints_match() -> None:
    """查询参数的取值约束一致：`limit.minimum`、`after.pattern`、路径参数 `id` 的 uuid 形状。"""
    contract_params = _contract_parameters("/v1/messages/{id}/replies")
    generated_params = {
        param["name"]: param for param in _generated_parameters("/v1/messages/{id}/replies")
    }
    assert set(generated_params) == set(contract_params)
    for name, expected in contract_params.items():
        expected_schema = expected.get("schema", {})
        actual_schema = generated_params[name].get("schema", {})
        if "minimum" in expected_schema:
            assert _walk_minimum(actual_schema) == {expected_schema["minimum"]}
        if "pattern" in expected_schema:
            assert _walk_patterns(actual_schema) == {expected_schema["pattern"]}
        assert generated_params[name]["required"] == expected.get("required", False)
    path_params = _generated_parameters("/v1/messages/{id}")
    assert [param["name"] for param in path_params] == ["id"]
    assert _walk_types(path_params[0]["schema"]) == {"string"}
    assert path_params[0]["schema"].get("format") == "uuid"


def test_body_field_constraints_match() -> None:
    """请求体字符串字段的取值约束一致：`minLength` 与 `recipient` 的 `pattern`。

    这三条是契约里写明的 422 触发条件（缺值、空串、地址形态非法）；它们只写在契约里，
    实现侧从 `RECIPIENT_PATTERN` 生成，因此必须两份文档逐字对上。
    """
    contract = _contract()["components"]["schemas"]["SubmitMessageRequest"]["properties"]
    generated = _generated()["components"]["schemas"]["SubmitMessageRequest"]["properties"]
    for name in ("recipient", "subject", "body"):
        assert generated[name].get("minLength") == contract[name].get("minLength") == 1
    assert generated["recipient"].get("pattern") == contract["recipient"].get("pattern")


def test_request_tolerates_unknown_properties() -> None:
    """契约写明未知字段一律忽略（调用方指定发件人的字段也不生效）：生成文档不得禁止它。

    JSON Schema 的默认语义是「未声明 `additionalProperties` 即允许」，因此缺省与显式 `true`
    等价；只有显式 `false` 才与契约冲突（`AC-4` 的接口面）。
    """
    expected = _contract()["components"]["schemas"]["SubmitMessageRequest"].get(
        "additionalProperties"
    )
    assert expected is True
    actual = _generated()["components"]["schemas"]["SubmitMessageRequest"].get(
        "additionalProperties", True
    )
    assert actual is True


def _is_ref(node: dict[str, Any], schema_name: str) -> bool:
    """该契约字段是否（经 `allOf` / `oneOf` / `anyOf`）引用给定 schema。"""
    found = _resolve_contract_ref(node, _contract())["branches"]
    return any(
        isinstance(branch.get("$ref"), str) and branch["$ref"].endswith("/" + schema_name)
        for branch in found
    )


def _nullable_types(node: dict[str, Any]) -> set[str]:
    """契约节点是否给了 `null` 分支：给了就回 `{"null"}`，否则回空集。"""
    branches = _resolve_contract_ref(node, _contract())["branches"]
    return {"null"} if any(branch.get("type") == "null" for branch in branches) else set()


def _walk_types(node: Any) -> set[str]:
    """收集节点里出现的全部 `type`（`anyOf` 的各分支一并收）。"""
    found: set[str] = set()
    if isinstance(node, dict):
        if isinstance(node.get("type"), str):
            found.add(node["type"])
        for value in node.values():
            found |= _walk_types(value)
    elif isinstance(node, list):
        for item in node:
            found |= _walk_types(item)
    return found


def _walk_minimum(node: Any) -> set[int]:
    """收集节点里出现的全部 `minimum`。"""
    found: set[int] = set()
    if isinstance(node, dict):
        if isinstance(node.get("minimum"), int):
            found.add(node["minimum"])
        for value in node.values():
            found |= _walk_minimum(value)
    elif isinstance(node, list):
        for item in node:
            found |= _walk_minimum(item)
    return found


def _walk_patterns(node: Any) -> set[str]:
    """收集节点里出现的全部 `pattern`。"""
    found: set[str] = set()
    if isinstance(node, dict):
        if isinstance(node.get("pattern"), str):
            found.add(node["pattern"])
        for value in node.values():
            found |= _walk_patterns(value)
    elif isinstance(node, list):
        for item in node:
            found |= _walk_patterns(item)
    return found


def _contract_parameters(path: str) -> dict[str, dict[str, Any]]:
    operation = _contract()["paths"][path]["get"]
    params: dict[str, dict[str, Any]] = {}
    for node in operation.get("parameters", []):
        if isinstance(node.get("$ref"), str):
            name = node["$ref"].rsplit("/", 1)[-1]
            node = _contract()["components"]["parameters"][name]
        params[node["name"]] = node
    return params


def _generated_parameters(path: str) -> list[dict[str, Any]]:
    return _generated()["paths"][path]["get"].get("parameters", [])
