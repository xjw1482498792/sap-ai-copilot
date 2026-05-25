"""
SAP 核心表结构定义（10 张表）。

字段名严格遵循真实 SAP 命名（VBELN/MATNR/KUNNR 等德文缩写），
这是项目的核心差异化 —— 通用 Text-to-SQL 模型在这种命名上效果差，
我们的 RAG + Prompt 工程要解决这个问题。

后续 Day 3-5 做 RAG 时，每张表的 description + columns 会被 embedding。
"""
from typing import TypedDict


class Column(TypedDict):
    name: str
    type: str         # SQLite 类型：TEXT / INTEGER / REAL / DATE
    desc: str         # 中文业务含义
    pk: bool


class Table(TypedDict):
    name: str
    desc: str         # 中文业务含义（用于 RAG 召回）
    module: str       # SAP 模块：SD / MM / FI
    columns: list[Column]


def _col(name, type_, desc, pk=False) -> Column:
    return {"name": name, "type": type_, "desc": desc, "pk": pk}


SCHEMAS: list[Table] = [
    # ---------- SD 销售与分销 ----------
    {
        "name": "VBAK",
        "desc": "销售订单抬头（Sales Order Header）。每张销售订单的主信息，一对多关联 VBAP。",
        "module": "SD",
        "columns": [
            _col("VBELN", "TEXT", "销售订单号", pk=True),
            _col("ERDAT", "DATE", "订单创建日期"),
            _col("ERNAM", "TEXT", "创建人"),
            _col("AUART", "TEXT", "订单类型，如 OR(标准)/RE(退货)"),
            _col("VKORG", "TEXT", "销售组织"),
            _col("VTWEG", "TEXT", "分销渠道"),
            _col("SPART", "TEXT", "产品组"),
            _col("KUNNR", "TEXT", "售达方客户编号，外键→KNA1.KUNNR"),
            _col("NETWR", "REAL", "订单净额（不含税）"),
            _col("WAERK", "TEXT", "订单币种，如 CNY/USD/EUR"),
        ],
    },
    {
        "name": "VBAP",
        "desc": "销售订单行项目（Sales Order Item）。一张订单可有多行，每行一种物料。",
        "module": "SD",
        "columns": [
            _col("VBELN", "TEXT", "销售订单号，外键→VBAK.VBELN", pk=True),
            _col("POSNR", "TEXT", "行项目号，如 000010/000020", pk=True),
            _col("MATNR", "TEXT", "物料编号，外键→MARA.MATNR"),
            _col("ARKTX", "TEXT", "行项目短描述"),
            _col("KWMENG", "REAL", "订单数量"),
            _col("VRKME", "TEXT", "销售单位，如 PC/KG/L"),
            _col("NETWR", "REAL", "行项目净额"),
            _col("WAERK", "TEXT", "币种"),
            _col("WERKS", "TEXT", "工厂代码"),
        ],
    },
    {
        "name": "KNA1",
        "desc": "客户主数据（Customer Master General Data）。所有客户的基础信息。",
        "module": "SD",
        "columns": [
            _col("KUNNR", "TEXT", "客户编号", pk=True),
            _col("NAME1", "TEXT", "客户名称"),
            _col("LAND1", "TEXT", "国家代码，如 CN/US/DE"),
            _col("ORT01", "TEXT", "城市"),
            _col("REGIO", "TEXT", "省/州"),
            _col("STRAS", "TEXT", "街道地址"),
            _col("PSTLZ", "TEXT", "邮编"),
            _col("TELF1", "TEXT", "电话"),
            _col("ERDAT", "DATE", "客户创建日期"),
        ],
    },
    # ---------- MM 物料管理 ----------
    {
        "name": "MARA",
        "desc": "物料主数据通用视图（Material Master General）。所有物料的基础信息，不含描述文本。",
        "module": "MM",
        "columns": [
            _col("MATNR", "TEXT", "物料编号", pk=True),
            _col("MTART", "TEXT", "物料类型，如 FERT(成品)/ROH(原料)/HALB(半成品)"),
            _col("MATKL", "TEXT", "物料组"),
            _col("MEINS", "TEXT", "基本计量单位"),
            _col("BRGEW", "REAL", "毛重"),
            _col("NTGEW", "REAL", "净重"),
            _col("GEWEI", "TEXT", "重量单位，如 KG/G"),
            _col("ERSDA", "DATE", "物料创建日期"),
            _col("ERNAM", "TEXT", "创建人"),
        ],
    },
    {
        "name": "MAKT",
        "desc": "物料描述（Material Descriptions）。多语言文本，一个物料一种语言一行。查物料名称必须 JOIN 这张表。",
        "module": "MM",
        "columns": [
            _col("MATNR", "TEXT", "物料编号，外键→MARA.MATNR", pk=True),
            _col("SPRAS", "TEXT", "语言代码，1=中文 E=英文 D=德文", pk=True),
            _col("MAKTX", "TEXT", "物料描述文本"),
        ],
    },
    {
        "name": "LFA1",
        "desc": "供应商主数据（Vendor Master）。所有供应商的基础信息。",
        "module": "MM",
        "columns": [
            _col("LIFNR", "TEXT", "供应商编号", pk=True),
            _col("NAME1", "TEXT", "供应商名称"),
            _col("LAND1", "TEXT", "国家代码"),
            _col("ORT01", "TEXT", "城市"),
            _col("STRAS", "TEXT", "街道地址"),
            _col("TELF1", "TEXT", "电话"),
            _col("ERDAT", "DATE", "创建日期"),
        ],
    },
    {
        "name": "EKKO",
        "desc": "采购订单抬头（Purchase Order Header）。一对多关联 EKPO。",
        "module": "MM",
        "columns": [
            _col("EBELN", "TEXT", "采购订单号", pk=True),
            _col("BUKRS", "TEXT", "公司代码"),
            _col("BSART", "TEXT", "采购订单类型，如 NB(标准)/UB(库存转移)"),
            _col("LIFNR", "TEXT", "供应商编号，外键→LFA1.LIFNR"),
            _col("WAERS", "TEXT", "币种"),
            _col("AEDAT", "DATE", "采购订单创建日期"),
            _col("ERNAM", "TEXT", "创建人"),
        ],
    },
    {
        "name": "EKPO",
        "desc": "采购订单行项目（Purchase Order Item）。",
        "module": "MM",
        "columns": [
            _col("EBELN", "TEXT", "采购订单号，外键→EKKO.EBELN", pk=True),
            _col("EBELP", "TEXT", "行项目号", pk=True),
            _col("MATNR", "TEXT", "物料编号，外键→MARA.MATNR"),
            _col("TXZ01", "TEXT", "行项目短描述"),
            _col("WERKS", "TEXT", "工厂"),
            _col("MENGE", "REAL", "采购数量"),
            _col("MEINS", "TEXT", "采购单位"),
            _col("NETPR", "REAL", "单价（净）"),
            _col("WAERS", "TEXT", "币种"),
        ],
    },
    # ---------- FI 财务 ----------
    {
        "name": "BKPF",
        "desc": "财务凭证抬头（Accounting Document Header）。一对多关联 BSEG。",
        "module": "FI",
        "columns": [
            _col("BUKRS", "TEXT", "公司代码", pk=True),
            _col("BELNR", "TEXT", "财务凭证号", pk=True),
            _col("GJAHR", "INTEGER", "会计年度", pk=True),
            _col("BLART", "TEXT", "凭证类型，如 SA(总账)/DR(客户发票)/KR(供应商发票)"),
            _col("BLDAT", "DATE", "凭证日期"),
            _col("BUDAT", "DATE", "过账日期"),
            _col("WAERS", "TEXT", "凭证币种"),
            _col("USNAM", "TEXT", "过账人"),
        ],
    },
    {
        "name": "BSEG",
        "desc": "财务凭证行项目（Accounting Document Segment）。一笔财务凭证可有多行借贷。",
        "module": "FI",
        "columns": [
            _col("BUKRS", "TEXT", "公司代码", pk=True),
            _col("BELNR", "TEXT", "财务凭证号，外键→BKPF.BELNR", pk=True),
            _col("GJAHR", "INTEGER", "会计年度", pk=True),
            _col("BUZEI", "INTEGER", "行项目号", pk=True),
            _col("KOART", "TEXT", "科目类型，D=客户 K=供应商 S=总账 A=资产 M=物料"),
            _col("SHKZG", "TEXT", "借贷标识，S=借方 H=贷方"),
            _col("DMBTR", "REAL", "本位币金额"),
            _col("WRBTR", "REAL", "凭证币种金额"),
            _col("WAERS", "TEXT", "币种"),
        ],
    },
]


def schema_to_ddl(table: Table) -> str:
    """生成 SQLite CREATE TABLE 语句"""
    cols = []
    for c in table["columns"]:
        cols.append(f'    "{c["name"]}" {c["type"]}')
    pks = [c["name"] for c in table["columns"] if c["pk"]]
    pk_clause = f',\n    PRIMARY KEY ({", ".join(pks)})' if pks else ""
    return f'CREATE TABLE IF NOT EXISTS "{table["name"]}" (\n' + ",\n".join(cols) + pk_clause + "\n);"


def schema_to_prompt_text(table: Table) -> str:
    """生成给 LLM 看的表描述（精简版）"""
    lines = [f'表 {table["name"]} ({table["module"]} 模块): {table["desc"]}']
    for c in table["columns"]:
        pk_mark = " [PK]" if c["pk"] else ""
        lines.append(f'  - {c["name"]} ({c["type"]}){pk_mark}: {c["desc"]}')
    return "\n".join(lines)


def all_schemas_prompt() -> str:
    """所有表的 prompt 文本拼接（Day 1 用，Day 3 RAG 之后会改）"""
    return "\n\n".join(schema_to_prompt_text(t) for t in SCHEMAS)


def get_table(name: str) -> Table | None:
    for t in SCHEMAS:
        if t["name"] == name.upper():
            return t
    return None
