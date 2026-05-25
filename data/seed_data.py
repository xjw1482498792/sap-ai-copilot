"""
生成符合 SAP 业务规则的 mock 数据。

数据量（约 3 万行）：
  - KNA1 客户主数据      : 100
  - LFA1 供应商主数据    : 50
  - MARA 物料主数据      : 200
  - MAKT 物料描述        : 400  (每物料中英 2 条)
  - VBAK 销售订单抬头    : 5000
  - VBAP 销售订单行项目  : ~15000 (每单 1-5 行)
  - EKKO 采购订单抬头    : 3000
  - EKPO 采购订单行项目  : ~8000
  - BKPF 财务凭证抬头    : 2000
  - BSEG 财务凭证行项目  : ~6000

运行方式：
    python -m data.seed_data
"""
import random
import sys
from datetime import date, timedelta

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from faker import Faker

from src.db import get_conn, init_schema

fake_cn = Faker("zh_CN")
fake_en = Faker("en_US")
Faker.seed(42)
random.seed(42)


# ---------- 业务规则常量 ----------
DATE_START = date(2024, 1, 1)
DATE_END = date(2026, 5, 20)        # 与当前日期对齐，便于"上个月"这类查询
COUNTRIES = ["CN", "CN", "CN", "US", "DE", "JP"]   # CN 权重大
PROVINCES = ["上海", "北京", "广东", "江苏", "浙江", "山东", "四川"]
CURRENCIES = ["CNY", "CNY", "CNY", "USD", "EUR"]
SALES_ORG = ["1000", "1000", "2000", "3000"]
DIST_CHANNEL = ["10", "20", "30"]
DIVISIONS = ["00", "01", "02"]
SALES_DOC_TYPE = ["OR", "OR", "OR", "RE", "QT"]    # OR 标准订单权重大
PO_TYPES = ["NB", "NB", "NB", "UB", "FO"]
MAT_TYPES = ["FERT", "FERT", "ROH", "HALB", "HAWA"]
MAT_GROUPS = ["001", "002", "003", "004", "005"]
UNITS = ["PC", "PC", "KG", "L", "M", "BOX"]
PLANTS = ["1000", "2000", "3000"]
USERS = ["LISI", "ZHANGSAN", "WANGWU", "ADMIN"]
FI_DOC_TYPES = ["SA", "DR", "DR", "KR", "AB"]
COMPANY_CODES = ["1000", "2000"]


def random_date(start=DATE_START, end=DATE_END) -> date:
    delta = (end - start).days
    return start + timedelta(days=random.randint(0, delta))


# ---------- 主数据 ----------
def gen_customers(n=100):
    rows = []
    for i in range(n):
        rows.append((
            f"C{10000+i:06d}",                          # KUNNR
            fake_cn.company(),                          # NAME1
            random.choice(COUNTRIES),                   # LAND1
            fake_cn.city_name(),                        # ORT01
            random.choice(PROVINCES),                   # REGIO
            fake_cn.street_address(),                   # STRAS
            fake_cn.postcode(),                         # PSTLZ
            fake_cn.phone_number(),                     # TELF1
            random_date().isoformat(),                  # ERDAT
        ))
    return rows


def gen_vendors(n=50):
    rows = []
    for i in range(n):
        rows.append((
            f"V{10000+i:06d}",
            fake_cn.company() + random.choice(["有限公司", "股份有限公司", "贸易公司"]),
            random.choice(COUNTRIES),
            fake_cn.city_name(),
            fake_cn.street_address(),
            fake_cn.phone_number(),
            random_date().isoformat(),
        ))
    return rows


PRODUCT_NAMES_CN = [
    "工业轴承", "不锈钢板材", "铝合金型材", "液压油泵", "PLC 控制器",
    "电机驱动器", "传感器模组", "光伏组件", "锂电池", "PCB 电路板",
    "数控刀具", "气动元件", "齿轮箱", "减速机", "电缆线",
    "包装纸箱", "化学试剂", "高纯硅片", "陶瓷基板", "精密弹簧",
]


def gen_materials(n=200):
    mara_rows = []
    makt_rows = []
    for i in range(n):
        matnr = f"M{100000+i:08d}"
        mat_type = random.choice(MAT_TYPES)
        gross = round(random.uniform(0.1, 500), 2)
        net = round(gross * random.uniform(0.85, 0.99), 2)
        mara_rows.append((
            matnr, mat_type, random.choice(MAT_GROUPS), random.choice(UNITS),
            gross, net, "KG",
            random_date(DATE_START, DATE_END - timedelta(days=30)).isoformat(),
            random.choice(USERS),
        ))
        # 中英两条描述
        base_name = random.choice(PRODUCT_NAMES_CN)
        suffix = f"型号-{random.randint(100, 999)}"
        makt_rows.append((matnr, "1", f"{base_name} {suffix}"))    # 中文
        makt_rows.append((matnr, "E", f"{fake_en.word().title()} {suffix}"))  # 英文
    return mara_rows, makt_rows


# ---------- 业务数据 ----------
def gen_sales_orders(n_orders, customer_ids, material_ids):
    vbak_rows = []
    vbap_rows = []
    for i in range(n_orders):
        vbeln = f"{50000000+i:010d}"
        erdat = random_date()
        kunnr = random.choice(customer_ids)
        currency = random.choice(CURRENCIES)
        # 1-5 行
        n_items = random.choices([1, 2, 3, 4, 5], weights=[2, 3, 3, 1, 1])[0]
        total = 0.0
        for j in range(n_items):
            matnr = random.choice(material_ids)
            qty = random.choice([1, 2, 5, 10, 50, 100, 500])
            unit_price = round(random.uniform(10, 5000), 2)
            net = round(qty * unit_price, 2)
            total += net
            vbap_rows.append((
                vbeln, f"{(j+1)*10:06d}", matnr,
                f"行项目 {j+1}", qty, random.choice(UNITS),
                net, currency, random.choice(PLANTS),
            ))
        vbak_rows.append((
            vbeln, erdat.isoformat(), random.choice(USERS),
            random.choice(SALES_DOC_TYPE), random.choice(SALES_ORG),
            random.choice(DIST_CHANNEL), random.choice(DIVISIONS),
            kunnr, round(total, 2), currency,
        ))
    return vbak_rows, vbap_rows


def gen_purchase_orders(n_orders, vendor_ids, material_ids):
    ekko_rows = []
    ekpo_rows = []
    for i in range(n_orders):
        ebeln = f"{4500000000+i:010d}"
        ekko_rows.append((
            ebeln, random.choice(COMPANY_CODES), random.choice(PO_TYPES),
            random.choice(vendor_ids), random.choice(CURRENCIES),
            random_date().isoformat(), random.choice(USERS),
        ))
        n_items = random.choices([1, 2, 3, 4], weights=[3, 4, 2, 1])[0]
        for j in range(n_items):
            matnr = random.choice(material_ids)
            qty = random.choice([10, 50, 100, 500, 1000])
            price = round(random.uniform(5, 2000), 2)
            ekpo_rows.append((
                ebeln, f"{(j+1)*10:05d}", matnr, f"采购物料 {matnr[-4:]}",
                random.choice(PLANTS), qty, random.choice(UNITS),
                price, random.choice(CURRENCIES),
            ))
    return ekko_rows, ekpo_rows


def gen_fi_docs(n_docs):
    bkpf_rows = []
    bseg_rows = []
    for i in range(n_docs):
        bukrs = random.choice(COMPANY_CODES)
        belnr = f"{4900000000+i:010d}"
        gjahr = random.choice([2024, 2025, 2026])
        bldat = random_date()
        budat = bldat + timedelta(days=random.randint(0, 5))
        currency = random.choice(CURRENCIES)
        bkpf_rows.append((
            bukrs, belnr, gjahr, random.choice(FI_DOC_TYPES),
            bldat.isoformat(), budat.isoformat(), currency,
            random.choice(USERS),
        ))
        # 借贷必须平衡：先生成借方再生成等额贷方
        n_pairs = random.choice([1, 1, 2])
        for p in range(n_pairs):
            amt = round(random.uniform(1000, 100000), 2)
            bseg_rows.append((
                bukrs, belnr, gjahr, p * 2 + 1,
                random.choice(["S", "D", "K"]), "S", amt, amt, currency,
            ))
            bseg_rows.append((
                bukrs, belnr, gjahr, p * 2 + 2,
                random.choice(["S", "D", "K"]), "H", amt, amt, currency,
            ))
    return bkpf_rows, bseg_rows


# ---------- 主流程 ----------
def main():
    print("[1/4] 初始化数据库 schema...")
    init_schema()

    print("[2/4] 生成主数据...")
    customers = gen_customers(100)
    vendors = gen_vendors(50)
    mara_rows, makt_rows = gen_materials(200)
    print(f"      客户 {len(customers)} / 供应商 {len(vendors)} / 物料 {len(mara_rows)}")

    print("[3/4] 生成业务数据...")
    customer_ids = [r[0] for r in customers]
    vendor_ids = [r[0] for r in vendors]
    material_ids = [r[0] for r in mara_rows]
    vbak, vbap = gen_sales_orders(5000, customer_ids, material_ids)
    ekko, ekpo = gen_purchase_orders(3000, vendor_ids, material_ids)
    bkpf, bseg = gen_fi_docs(2000)
    print(f"      销售单 {len(vbak)} 抬头 / {len(vbap)} 行")
    print(f"      采购单 {len(ekko)} 抬头 / {len(ekpo)} 行")
    print(f"      财务凭证 {len(bkpf)} 抬头 / {len(bseg)} 行")

    print("[4/4] 写入数据库...")
    conn = get_conn()
    cur = conn.cursor()

    def bulk(sql, rows):
        cur.executemany(sql, rows)

    bulk("INSERT OR REPLACE INTO KNA1 VALUES (?,?,?,?,?,?,?,?,?)", customers)
    bulk("INSERT OR REPLACE INTO LFA1 VALUES (?,?,?,?,?,?,?)", vendors)
    bulk("INSERT OR REPLACE INTO MARA VALUES (?,?,?,?,?,?,?,?,?)", mara_rows)
    bulk("INSERT OR REPLACE INTO MAKT VALUES (?,?,?)", makt_rows)
    bulk("INSERT OR REPLACE INTO VBAK VALUES (?,?,?,?,?,?,?,?,?,?)", vbak)
    bulk("INSERT OR REPLACE INTO VBAP VALUES (?,?,?,?,?,?,?,?,?)", vbap)
    bulk("INSERT OR REPLACE INTO EKKO VALUES (?,?,?,?,?,?,?)", ekko)
    bulk("INSERT OR REPLACE INTO EKPO VALUES (?,?,?,?,?,?,?,?,?)", ekpo)
    bulk("INSERT OR REPLACE INTO BKPF VALUES (?,?,?,?,?,?,?,?)", bkpf)
    bulk("INSERT OR REPLACE INTO BSEG VALUES (?,?,?,?,?,?,?,?,?)", bseg)

    conn.commit()
    conn.close()
    print("完成。数据库文件已生成。")


if __name__ == "__main__":
    main()
