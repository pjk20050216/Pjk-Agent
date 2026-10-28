"""风控域确定性数据源 —— 用于离线演示、回测与测试。

生产接入时，把本模块的函数替换为对 Java 风控平台 / 特征平台的调用即可，
工具层契约（返回结构）保持不变。所有随机均带固定种子，保证结果可复现。
"""
from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta, timezone
from typing import Any

import yaml


FEATURES: dict[str, dict[str, Any]] = {
    "device_risk_score": {
        "desc": "设备风险评分（0-1，越高越危险）",
        "mean": 0.32, "p50": 0.27, "p90": 0.68, "p99": 0.91,
        "null_rate": 0.012, "psi": 0.08, "updated": "T+1",
        "segments": {"APP": 0.35, "H5": 0.29, "PC": 0.22},
    },
    "login_fail_count_1h": {
        "desc": "1 小时内登录失败次数",
        "mean": 1.4, "p50": 0.0, "p90": 4.0, "p99": 9.0,
        "null_rate": 0.002, "psi": 0.12, "updated": "实时",
        "segments": {"APP": 1.6, "H5": 1.3, "PC": 1.1},
    },
    "txn_amount_1d": {
        "desc": "近 1 日交易金额（元）",
        "mean": 860.0, "p50": 320.0, "p90": 2100.0, "p99": 8600.0,
        "null_rate": 0.004, "psi": 0.31, "updated": "T+1",
        "segments": {"APP": 920.0, "H5": 640.0, "PC": 780.0},
    },
    "user_age_days": {
        "desc": "用户注册天数",
        "mean": 420.0, "p50": 180.0, "p90": 1200.0, "p99": 2600.0,
        "null_rate": 0.0, "psi": 0.05, "updated": "T+1",
        "segments": {"APP": 380.0, "H5": 460.0, "PC": 520.0},
    },
    "merchant_risk_level": {
        "desc": "商户风险等级（1-5）",
        "mean": 2.1, "p50": 2.0, "p90": 4.0, "p99": 5.0,
        "null_rate": 0.021, "psi": 0.19, "updated": "T+1",
        "segments": {"APP": 2.0, "H5": 2.3, "PC": 2.4},
    },
    "same_device_user_count_7d": {
        "desc": "7 日内同设备关联用户数",
        "mean": 1.8, "p50": 1.0, "p90": 3.0, "p99": 12.0,
        "null_rate": 0.006, "psi": 0.44, "updated": "T+1",
        "segments": {"APP": 1.9, "H5": 1.7, "PC": 1.5},
    },
}


RULE_V1 = """rule_id: R_RISK_001
name: 高风险设备登录拦截
version: 1
priority: 100
enabled: true
scope:
  channels: [APP, H5]
  products: [credit_pay]
when:
  all:
    - feature: device_risk_score
      op: gte
      value: 0.90
then:
  action: reject
  reason_code: DEVICE_HIGH_RISK
  tag: device_fraud
gray:
  percent: 0
  whitelist: []
"""

RULE_V2 = """rule_id: R_RISK_001
name: 高风险设备登录拦截
version: 2
priority: 100
enabled: true
scope:
  channels: [APP, H5]
  products: [credit_pay]
when:
  all:
    - feature: device_risk_score
      op: gte
      value: 0.82
    - feature: login_fail_count_1h
      op: gte
      value: 5
then:
  action: reject
  reason_code: DEVICE_HIGH_RISK
  tag: device_fraud
gray:
  percent: 5
  whitelist: []
"""

RULE_V3_DRAFT = """rule_id: R_RISK_001
name: 高风险设备登录拦截
version: 3
priority: 100
enabled: true
scope:
  channels: [APP, H5]
  products: [credit_pay]
when:
  any:
    - all:
        - feature: device_risk_score
          op: gte
          value: 0.85
        - feature: login_fail_count_1h
          op: gte
          value: 3
    - all:
        - feature: same_device_user_count_7d
          op: gte
          value: 8
        - feature: user_age_days
          op: lt
          value: 7
then:
  action: review
  reason_code: DEVICE_SUSPECT_REVIEW
  tag: device_fraud
gray:
  percent: 1
  whitelist: []
"""

RULE_TXN = """rule_id: R_TXN_014
name: 大额交易人工复核
version: 4
priority: 80
enabled: true
scope:
  channels: [APP, H5, PC]
  products: [credit_pay, debit_pay]
when:
  all:
    - feature: txn_amount_1d
      op: gte
      value: 8000
    - feature: merchant_risk_level
      op: gte
      value: 4
then:
  action: review
  reason_code: TXN_LARGE_AMOUNT
  tag: amount_risk
gray:
  percent: 20
  whitelist: []
"""

RULES: dict[str, dict[str, Any]] = {
    "R_RISK_001": {
        "rule_id": "R_RISK_001",
        "name": "高风险设备登录拦截",
        "active_version": 2,
        "gray_percent": 5,
        "owner": "反欺诈策略组",
        "versions": {
            1: {"dsl": RULE_V1, "status": "archived", "published_at": "2026-08-12T10:00:00+08:00"},
            2: {"dsl": RULE_V2, "status": "gray", "published_at": "2026-10-18T20:30:00+08:00"},
            3: {"dsl": RULE_V3_DRAFT, "status": "draft", "published_at": None},
        },
        "depends_on": ["device_risk_score", "login_fail_count_1h", "same_device_user_count_7d", "user_age_days"],
    },
    "R_TXN_014": {
        "rule_id": "R_TXN_014",
        "name": "大额交易人工复核",
        "active_version": 4,
        "gray_percent": 20,
        "owner": "交易风控组",
        "versions": {
            3: {
                "dsl": RULE_TXN.replace("version: 4", "version: 3").replace("value: 8000", "value: 10000"),
                "status": "archived",
                "published_at": "2026-09-01T09:00:00+08:00",
            },
            4: {"dsl": RULE_TXN, "status": "active", "published_at": "2026-10-20T14:00:00+08:00"},
        },
        "depends_on": ["txn_amount_1d", "merchant_risk_level"],
    },
}


def _seed(*parts: str) -> int:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def get_rule(rule_id: str, version: int | None = None) -> dict[str, Any] | None:
    rule = RULES.get(rule_id)
    if rule is None:
        return None
    target = version or rule["active_version"]
    payload = rule["versions"].get(target)
    if payload is None:
        return None
    return {
        "rule_id": rule_id,
        "name": rule["name"],
        "version": target,
        "status": payload["status"],
        "published_at": payload["published_at"],
        "gray_percent": rule["gray_percent"],
        "owner": rule["owner"],
        "depends_on": rule["depends_on"],
        "dsl": payload["dsl"],
        "versions": sorted(rule["versions"].keys()),
    }


def list_rules() -> list[dict[str, Any]]:
    return [
        {
            "rule_id": r["rule_id"],
            "name": r["name"],
            "active_version": r["active_version"],
            "gray_percent": r["gray_percent"],
            "owner": r["owner"],
            "status": r["versions"][r["active_version"]]["status"],
        }
        for r in RULES.values()
    ]


def feature_stats(feature: str) -> dict[str, Any] | None:
    meta = FEATURES.get(feature)
    if meta is None:
        return None
    return {"feature": feature, **meta}


def query_feature(
    feature: str,
    segment: str = "all",
    window: str = "7d",
) -> dict[str, Any] | None:
    meta = FEATURES.get(feature)
    if meta is None:
        return None
    value = meta["mean"] if segment == "all" else meta["segments"].get(segment)
    if value is None:
        return None
    rng = random.Random(_seed(feature, segment, window))
    return {
        "feature": feature,
        "desc": meta["desc"],
        "segment": segment,
        "window": window,
        "mean": round(value, 4),
        "p50": meta["p50"],
        "p90": meta["p90"],
        "p99": meta["p99"],
        "null_rate": meta["null_rate"],
        "psi": meta["psi"],
        "updated": meta["updated"],
        "sample_size": rng.randint(8000, 60000),
        "anomaly": meta["psi"] > 0.25,
        "anomaly_hint": (
            "PSI 超过 0.25，存在显著分布漂移，建议核对上游口径"
            if meta["psi"] > 0.25
            else "分布稳定"
        ),
    }


def backtest(
    rule_id: str,
    *,
    sample_size: int = 2000,
    version: int | None = None,
) -> dict[str, Any]:
    rule = RULES.get(rule_id)
    if rule is None:
        return {"error": f"rule not found: {rule_id}"}
    target = version or rule["active_version"]
    base_version = max(v for v in rule["versions"] if v < target) if any(
        v < target for v in rule["versions"]
    ) else target
    rng = random.Random(_seed(rule_id, str(target), str(sample_size)))

    total = max(100, sample_size)
    base_hit = int(total * rng.uniform(0.04, 0.09))
    new_hit = int(base_hit * rng.uniform(0.85, 1.35))
    base_fp = int(base_hit * rng.uniform(0.08, 0.16))
    new_fp = int(new_hit * rng.uniform(0.07, 0.2))
    base_fn = int(total * rng.uniform(0.004, 0.012))
    new_fn = int(total * rng.uniform(0.003, 0.014))
    amount_base = round(base_hit * rng.uniform(1200, 2600), 2)
    amount_new = round(new_hit * rng.uniform(1100, 2800), 2)

    return {
        "rule_id": rule_id,
        "base_version": base_version,
        "test_version": target,
        "sample_size": total,
        "sample_window": "2026-10-01 ~ 2026-10-20",
        "metrics": {
            "hit_count": {"before": base_hit, "after": new_hit, "delta": new_hit - base_hit},
            "false_positive": {"before": base_fp, "after": new_fp, "delta": new_fp - base_fp},
            "false_negative": {"before": base_fn, "after": new_fn, "delta": new_fn - base_fn},
            "hit_rate": {
                "before": round(base_hit / total, 4),
                "after": round(new_hit / total, 4),
            },
            "false_positive_rate": {
                "before": round(base_fp / total, 4),
                "after": round(new_fp / total, 4),
            },
            "amount_impact": {"before": amount_base, "after": amount_new},
        },
        "confidence": "low" if total < 1000 else "medium",
        "conclusion": (
            "误杀率上升超过 0.5 个百分点，不建议直接发布"
            if (new_fp - base_fp) / total > 0.005
            else "指标变化在可接受范围，建议小比例灰度验证"
        ),
        "diff_samples": [
            {
                "sample_id": f"S{rng.randint(100000, 999999)}",
                "before": "pass",
                "after": "reject",
                "reason": "device_risk_score 阈值下调",
            }
            for _ in range(5)
        ],
    }


def diff_rule(rule_id: str, from_version: int, to_version: int) -> str:
    import difflib

    rule = RULES.get(rule_id)
    if rule is None:
        return f"rule not found: {rule_id}"
    old = rule["versions"].get(from_version)
    new = rule["versions"].get(to_version)
    if old is None or new is None:
        return f"version not found: {from_version} or {to_version}"
    return "\n".join(
        difflib.unified_diff(
            old["dsl"].splitlines(),
            new["dsl"].splitlines(),
            fromfile=f"{rule_id}@v{from_version}",
            tofile=f"{rule_id}@v{to_version}",
            lineterm="",
        )
    )


def validate_rule(dsl: str) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        data = yaml.safe_load(dsl)
    except yaml.YAMLError as exc:
        return {"valid": False, "errors": [f"YAML 解析失败: {exc}"], "warnings": []}
    if not isinstance(data, dict):
        return {"valid": False, "errors": ["DSL 根节点必须是对象"], "warnings": []}

    for key in ("rule_id", "name", "version", "when", "then"):
        if key not in data:
            errors.append(f"缺少必填字段: {key}")

    when = data.get("when")
    if isinstance(when, dict):
        if not any(k in when for k in ("all", "any", "not")):
            errors.append("when 必须包含 all / any / not 之一")
        conditions = when.get("all") or when.get("any") or []
        if isinstance(conditions, list):
            for index, cond in enumerate(conditions):
                if isinstance(cond, dict) and "feature" in cond:
                    if cond["feature"] not in FEATURES:
                        errors.append(f"条件 {index}: 未注册特征 {cond['feature']}")
                    if cond.get("op") not in {
                        "eq", "neq", "gt", "gte", "lt", "lte",
                        "in", "not_in", "between", "is_null", "regex",
                    }:
                        errors.append(f"条件 {index}: 非法操作符 {cond.get('op')}")
    else:
        errors.append("when 必须是对象")

    then = data.get("then")
    if isinstance(then, dict):
        if then.get("action") not in {"reject", "review", "pass", "challenge"}:
            errors.append("then.action 必须是 reject/review/pass/challenge")
        if not then.get("reason_code"):
            warnings.append("建议补充 then.reason_code 便于下游归因")
    else:
        errors.append("then 必须是对象")

    gray = data.get("gray")
    if isinstance(gray, dict):
        percent = gray.get("percent", 0)
        if not isinstance(percent, (int, float)) or not 0 <= percent <= 100:
            errors.append("gray.percent 必须在 0-100 之间")
        elif percent > 20:
            warnings.append("灰度比例超过 20%，建议分阶段放量")

    if data.get("version") and isinstance(data["version"], int) and data["version"] < 1:
        errors.append("version 必须为正整数")

    return {"valid": not errors, "errors": errors, "warnings": warnings}


def search_logs(query: str, *, time_window: str = "1h", limit: int = 50) -> list[dict[str, Any]]:
    rng = random.Random(_seed(query, time_window))
    now = datetime.now(timezone.utc)
    decisions = ["reject", "review", "pass", "challenge"]
    reasons = [
        "设备风险分偏高", "登录失败次数过多", "同设备关联用户异常",
        "大额交易人工复核", "误杀申诉复核", "名单命中", "灰度批次放量",
    ]
    lines: list[dict[str, Any]] = []
    for i in range(min(limit, 200)):
        ts = now - timedelta(seconds=rng.randint(0, 3600))
        decision = rng.choice(decisions)
        rule_id = rng.choice(list(RULES))
        reason = rng.choice(reasons)
        line = (
            f"{ts.isoformat()} INFO decision={decision} rule={rule_id} "
            f"reason={reason} device_risk_score={rng.uniform(0.1, 0.99):.3f} "
            f"latency_ms={rng.randint(8, 240)} trace={rng.getrandbits(32):08x}"
        )
        if query and query.lower() not in line.lower() and query not in (rule_id, decision):
            continue
        lines.append(
            {
                "ts": ts.isoformat(),
                "decision": decision,
                "rule_id": rule_id,
                "latency_ms": rng.randint(8, 240),
                "line": line,
            }
        )
    return lines[:limit]
