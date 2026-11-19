"""预置报表目录。覆盖：

R1 行金额按品类        —— N:1 且维度键唯一，ATJ/DJA 都合法，朴素连接不放大
R2 订单金额按区域      —— 维度就在订单表上，无需连接，三条路径完全一致
R3 分摊金额按成本中心  —— 分摊表自身维度，最细粒度直接聚合
R4 订单行金额按活动    —— 经桥接表的 M:N 且无分摊基准，ATJ/DJA 双双拒绝
                        （朴素连接会放大，可直接定位重复贡献）
R5 行金额按品类(故障)  —— 与 R1 同结构，但开启"重复产品维度"场景后
                        运行期键不唯一，ATJ/DJA 双双拒绝并给出重复键样本
R6 订单金额按成本中心  —— 订单->订单行->分摊 的 1:N 链，带 weight 基准；
                        ATJ 拒绝（粗粒度度量不能跨扇出边先聚合），
                        DJA 合法且总量守恒，朴素连接把 3300 放大到 5400
"""
from __future__ import annotations

CATALOG = [
    {
        "report_name": "R1 行金额按品类",
        "fact_table": "order_lines",
        "measures": [{"col": "line_amt", "agg": "sum"},
                     {"col": "line_cnt", "agg": "sum"}],
        "group_by": ["category"],
        "description": "标准 N:1 维度关联，演示两条合法路径结果一致",
    },
    {
        "report_name": "R2 订单金额按区域",
        "fact_table": "orders",
        "measures": [{"col": "order_amt", "agg": "sum"},
                     {"col": "order_cnt", "agg": "sum"}],
        "group_by": ["region"],
        "description": "维度在事实表上，无连接路径，基线对照",
    },
    {
        "report_name": "R3 分摊金额按成本中心",
        "fact_table": "allocations",
        "measures": [{"col": "alloc_amt", "agg": "sum"}],
        "group_by": ["cost_center"],
        "description": "最细粒度直接聚合，分摊表自身就是目标粒度",
    },
    {
        "report_name": "R4 行金额按活动(M:N错误关联)",
        "fact_table": "order_lines",
        "measures": [{"col": "line_amt", "agg": "sum"},
                     {"col": "line_cnt", "agg": "sum"}],
        "group_by": ["campaign_name"],
        "description": "桥接表多对多且无分摊基准：合法路径拒绝，朴素连接放大",
    },
    {
        "report_name": "R5 行金额按品类(维度重复场景)",
        "fact_table": "order_lines",
        "measures": [{"col": "line_amt", "agg": "sum"}],
        "group_by": ["category"],
        "description": "开启 duplicate_product 后维度键不唯一，演示登记与运行期数据联动诊断",
    },
    {
        "report_name": "R6 订单金额按成本中心(粗粒度下推)",
        "fact_table": "order_lines",
        "measures": [{"col": "line_amt", "agg": "sum"}],
        "group_by": ["cost_center"],
        "description": "行金额沿带 weight 基准的 1:N 链下推到成本中心："
                       "ATJ 拒绝扇出、DJA 合法且总量守恒(3300)，朴素连接放大到 5400",
    },
]


def get_spec(name: str) -> dict:
    for s in CATALOG:
        if s["report_name"] == name:
            return dict(s)
    raise KeyError(name)
