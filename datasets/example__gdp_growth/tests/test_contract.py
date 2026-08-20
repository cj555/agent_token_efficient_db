# GENERATED from contract.yaml by `dw index` — 请勿手工编辑
"""example__gdp_growth 的合约一致性测试 —— 由 contract.yaml 生成，勿手改。"""
import pytest

import dwlib as dw

DATASET = "example__gdp_growth"

pytestmark = pytest.mark.skipif(
    not dw.exists(DATASET), reason=f"{DATASET} 尚无 curated 数据"
)


def test_contract_conformance():
    """列/类型/非空/唯一/grain/质量规则，全部按合约执行。"""
    res = dw.check(DATASET)
    errors = [i for i in res["issues"] if i["level"] == "error"]
    assert not errors, "\n".join(f'{i["code"]}: {i["msg"]}' for i in errors)
