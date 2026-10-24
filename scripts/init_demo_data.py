#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成「数析」演示数仓：``data/raw/*.csv`` + ``data/demo.db``。

固定随机种子 42，脚本可重复执行、结果确定性覆盖：
- stores(~30)：门店维度，region ∈ {华东, 华北, 华南, 西南, 华中}
- products(~40)：商品维度，5 个品类
- customers(~800)：客户维度，含敏感字段 name / phone（HITL 演示用）
- sales(~4000)：销售事实，日期 2025-01-01 ~ 2025-12-31，按月配置季节性（Q4 高）

只依赖标准库，``python scripts/init_demo_data.py`` 即可运行；
Agent / CLI / Streamlit 启动前都依赖本脚本产出的 demo.db。
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sqlite3
from datetime import date
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT_DIR / "data" / "raw"
DB_PATH = ROOT_DIR / "data" / "demo.db"
SEED = 42

N_STORES = 30
N_PRODUCTS = 40
N_CUSTOMERS = 800
N_SALES = 4000
START_DATE = date(2025, 1, 1)
END_DATE = date(2025, 12, 31)

REGION_CITIES: Dict[str, List[str]] = {
    "华东": ["上海", "杭州", "南京", "苏州", "宁波"],
    "华北": ["北京", "天津", "石家庄", "太原"],
    "华南": ["广州", "深圳", "东莞", "佛山"],
    "西南": ["成都", "重庆", "昆明", "贵阳"],
    "华中": ["武汉", "长沙", "郑州", "南昌"],
}
REGIONS = list(REGION_CITIES)

CHANNELS = ("线上", "线下")
CHANNEL_WEIGHTS = (0.45, 0.55)

CATEGORY_SPEC: Dict[str, Tuple[Tuple[float, float], float, List[str]]] = {
    # 品类: (单价区间, 成本占单价比例, 商品名称词根)
    "饮料": ((3.0, 16.0), 0.55, ["乌龙茶", "气泡水", "鲜榨果汁", "冷萃咖啡", "矿泉水", "乳酸菌"]),
    "零食": ((5.0, 45.0), 0.58, ["薯片", "坚果", "曲奇", "巧克力", "肉脯", "果干"]),
    "生鲜": ((8.0, 88.0), 0.66, ["红富士苹果", "土鸡蛋", "冷鲜牛肉", "鲈鱼", "西兰花", "鲜牛奶"]),
    "日化": ((15.0, 120.0), 0.52, ["洗发水", "洗衣液", "牙膏", "面巾纸", "洗手液", "沐浴露"]),
    "家电": ((199.0, 2999.0), 0.62, ["电饭煲", "吹风机", "加湿器", "电水壶", "扫地机", "护眼台灯"]),
}
CATEGORIES = list(CATEGORY_SPEC)
CATEGORY_WEIGHTS = {"饮料": 0.27, "零食": 0.25, "生鲜": 0.22, "日化": 0.16, "家电": 0.10}

MEMBER_LEVELS = ("普通", "银卡", "金卡", "钻石")
MEMBER_WEIGHTS = (0.50, 0.25, 0.18, 0.07)

SURNAMES = "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤"
GIVEN_NAMES = ["伟", "芳", "娜", "敏", "静", "丽", "强", "磊", "洋", "艳", "杰", "娟", "涛", "明", "超", "秀英", "华", "慧", "建", "平", "文", "晨", "欣", "宇", "浩", "雨", "婷", "琳", "鑫", "博"]
PHONE_PREFIXES = ("138", "139", "137", "136", "150", "151", "158", "159", "186", "188")

# 月度订单量权重：Q4（10/11/12 月）明显更高，保证环比/趋势有故事可讲
MONTH_WEIGHTS = {
    1: 0.72, 2: 0.62, 3: 0.90, 4: 0.88, 5: 0.95, 6: 0.92,
    7: 0.85, 8: 0.90, 9: 1.00, 10: 1.35, 11: 1.55, 12: 1.60,
}

SCHEMA_SQL = """
CREATE TABLE stores (
    store_id   TEXT PRIMARY KEY,
    store_name TEXT NOT NULL,
    region     TEXT NOT NULL,
    city       TEXT NOT NULL,
    channel    TEXT NOT NULL,
    open_date  TEXT NOT NULL
);
CREATE TABLE products (
    product_id   TEXT PRIMARY KEY,
    product_name TEXT NOT NULL,
    category     TEXT NOT NULL,
    unit_price   REAL NOT NULL,
    unit_cost    REAL NOT NULL
);
CREATE TABLE customers (
    customer_id   TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    gender        TEXT NOT NULL,
    age           INTEGER NOT NULL,
    member_level  TEXT NOT NULL,
    register_date TEXT NOT NULL,
    phone         TEXT NOT NULL
);
CREATE TABLE sales (
    order_id    TEXT PRIMARY KEY,
    order_date  TEXT NOT NULL,
    store_id    TEXT NOT NULL,
    product_id  TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    channel     TEXT NOT NULL,
    quantity    INTEGER NOT NULL,
    amount      REAL NOT NULL,
    cost        REAL NOT NULL,
    FOREIGN KEY (store_id)    REFERENCES stores(store_id),
    FOREIGN KEY (product_id)  REFERENCES products(product_id),
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
);
CREATE INDEX idx_sales_date     ON sales(order_date);
CREATE INDEX idx_sales_store    ON sales(store_id);
CREATE INDEX idx_sales_product  ON sales(product_id);
CREATE INDEX idx_sales_customer ON sales(customer_id);
CREATE INDEX idx_customers_register ON customers(register_date);
"""

STORES_HEADER = ["store_id", "store_name", "region", "city", "channel", "open_date"]
PRODUCTS_HEADER = ["product_id", "product_name", "category", "unit_price", "unit_cost"]
CUSTOMERS_HEADER = ["customer_id", "name", "gender", "age", "member_level", "register_date", "phone"]
SALES_HEADER = ["order_id", "order_date", "store_id", "product_id", "customer_id", "channel", "quantity", "amount", "cost"]


# --------------------------------------------------------------------------- #
# 数据生成
# --------------------------------------------------------------------------- #
def generate_stores(rng: random.Random) -> List[dict]:
    """30 家门店：区域/城市尽量均衡分布，渠道 线上/线下 混合."""
    stores: List[dict] = []
    cities: List[Tuple[str, str]] = []
    for region, region_cities in REGION_CITIES.items():
        for city in region_cities:
            cities.append((region, city))
    # 30 家店从 21 个城市里循环取，保证每个区域都至少 4 家店
    for index in range(N_STORES):
        region, city = cities[index % len(cities)]
        channel = rng.choices(CHANNELS, weights=CHANNEL_WEIGHTS, k=1)[0]
        open_date = date(2023, 1, 1) + rng.randrange(0, 730) * _one_day()
        stores.append(
            {
                "store_id": f"S{index + 1:03d}",
                "store_name": f"{city}·{region}{index + 1:02d}店",
                "region": region,
                "city": city,
                "channel": channel,
                "open_date": open_date.isoformat(),
            }
        )
    return stores


def generate_products(rng: random.Random) -> List[dict]:
    """40 个商品：每品类 8 个 SKU，单价按品类区间随机，成本必然低于单价."""
    products: List[dict] = []
    per_category = N_PRODUCTS // len(CATEGORIES)
    index = 0
    for category in CATEGORIES:
        (low, high), cost_ratio, roots = CATEGORY_SPEC[category]
        for slot in range(per_category):
            index += 1
            root = roots[slot % len(roots)]
            unit_price = round(rng.uniform(low, high), 2)
            unit_cost = round(unit_price * rng.uniform(max(cost_ratio - 0.10, 0.30), cost_ratio), 2)
            unit_cost = min(unit_cost, round(unit_price * 0.95, 2))
            products.append(
                {
                    "product_id": f"P{index:03d}",
                    "product_name": f"{root}{slot + 1:02d}",
                    "category": category,
                    "unit_price": unit_price,
                    "unit_cost": unit_cost,
                }
            )
    return products


def generate_customers(rng: random.Random) -> List[dict]:
    """800 位客户，含 name / phone 两个敏感字段（HITL 演示用）."""
    customers: List[dict] = []
    register_low = date(2024, 1, 1).toordinal()
    register_high = date(2025, 9, 30).toordinal()
    for index in range(N_CUSTOMERS):
        surname = rng.choice(SURNAMES)
        given = rng.choice(GIVEN_NAMES)
        if rng.random() < 0.35:
            given += rng.choice(GIVEN_NAMES)
        register_date = date.fromordinal(rng.randint(register_low, register_high))
        customers.append(
            {
                "customer_id": f"C{index + 1:04d}",
                "name": surname + given,
                "gender": rng.choices(("男", "女"), weights=(0.48, 0.52), k=1)[0],
                "age": rng.randint(18, 65),
                "member_level": rng.choices(MEMBER_LEVELS, weights=MEMBER_WEIGHTS, k=1)[0],
                "register_date": register_date.isoformat(),
                "phone": rng.choice(PHONE_PREFIXES) + f"{rng.randint(0, 99999999):08d}",
            }
        )
    return customers


def generate_sales(
    rng: random.Random,
    stores: Sequence[dict],
    products: Sequence[dict],
    customers: Sequence[dict],
) -> List[dict]:
    """4000 笔销售：按月分配订单量（Q4 高），金额 = 数量 × 单价 × 折扣(0.9~1.1)."""
    monthly_counts = _monthly_counts(N_SALES)
    product_weights = [CATEGORY_WEIGHTS[y["category"]] for y in products]
    quantity_choices = (1, 2, 3, 4, 5)
    quantity_weights = (0.40, 0.30, 0.18, 0.08, 0.04)

    raw_rows: List[dict] = []
    for month in range(1, 13):
        days_in_month = _days_in_month(2025, month)
        for _ in range(monthly_counts[month]):
            store = rng.choice(stores)
            product = rng.choices(products, weights=product_weights, k=1)[0]
            customer = rng.choice(customers)
            quantity = rng.choices(quantity_choices, weights=quantity_weights, k=1)[0]
            discount = rng.uniform(0.9, 1.1)
            amount = round(quantity * product["unit_price"] * discount, 2)
            cost = round(quantity * product["unit_cost"], 2)
            # 70% 的订单沿用门店主渠道，其余走另一个渠道
            channel = store["channel"] if rng.random() < 0.70 else _other_channel(store["channel"])
            raw_rows.append(
                {
                    "order_date": date(2025, month, rng.randint(1, days_in_month)).isoformat(),
                    "store_id": store["store_id"],
                    "product_id": product["product_id"],
                    "customer_id": customer["customer_id"],
                    "channel": channel,
                    "quantity": quantity,
                    "amount": amount,
                    "cost": cost,
                }
            )

    raw_rows.sort(key=lambda row: (row["order_date"], row["store_id"], row["product_id"]))
    sales: List[dict] = []
    for index, row in enumerate(raw_rows):
        ordered = {"order_id": f"O{index + 1:05d}"}
        ordered.update(row)
        sales.append(ordered)
    return sales


def _monthly_counts(total: int) -> Dict[int, int]:
    weight_sum = sum(MONTH_WEIGHTS.values())
    counts = {month: int(round(total * weight / weight_sum)) for month, weight in MONTH_WEIGHTS.items()}
    diff = total - sum(counts.values())
    counts[12] += diff  # 尾差放进 12 月，保证总量精确
    return counts


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - date(year, month, 1)).days


def _one_day():
    from datetime import timedelta

    return timedelta(days=1)


def _other_channel(channel: str) -> str:
    return "线下" if channel == "线上" else "线上"


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #
def _write_csv(path: Path, header: Sequence[str], rows: Sequence[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        for row in rows:
            writer.writerow([row[key] for key in header])


def _write_database(
    stores: Sequence[dict],
    products: Sequence[dict],
    customers: Sequence[dict],
    sales: Sequence[dict],
) -> Path:
    """先写临时文件再原子替换，避免中途失败留下半个数据库."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = DB_PATH.with_name(DB_PATH.name + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    connection = sqlite3.connect(str(tmp_path))
    try:
        connection.executescript(SCHEMA_SQL)
        connection.executemany(
            "INSERT INTO stores VALUES (?, ?, ?, ?, ?, ?)",
            [[row[key] for key in STORES_HEADER] for row in stores],
        )
        connection.executemany(
            "INSERT INTO products VALUES (?, ?, ?, ?, ?)",
            [[row[key] for key in PRODUCTS_HEADER] for row in products],
        )
        connection.executemany(
            "INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?, ?)",
            [[row[key] for key in CUSTOMERS_HEADER] for row in customers],
        )
        connection.executemany(
            "INSERT INTO sales VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [[row[key] for key in SALES_HEADER] for row in sales],
        )
        connection.commit()
    finally:
        connection.close()
    os.replace(str(tmp_path), str(DB_PATH))
    return DB_PATH


def init_demo_data(seed: int = SEED) -> dict:
    """生成全部演示数据并落盘，返回行数摘要（幂等：重复执行覆盖旧数据）."""
    rng = random.Random(seed)
    stores = generate_stores(rng)
    products = generate_products(rng)
    customers = generate_customers(rng)
    sales = generate_sales(rng, stores, products, customers)

    _write_csv(RAW_DIR / "stores.csv", STORES_HEADER, stores)
    _write_csv(RAW_DIR / "products.csv", PRODUCTS_HEADER, products)
    _write_csv(RAW_DIR / "customers.csv", CUSTOMERS_HEADER, customers)
    _write_csv(RAW_DIR / "sales.csv", SALES_HEADER, sales)
    db_path = _write_database(stores, products, customers, sales)

    return {
        "stores": len(stores),
        "products": len(products),
        "customers": len(customers),
        "sales": len(sales),
        "db_path": str(db_path),
        "raw_dir": str(RAW_DIR),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成「数析」演示数仓（CSV + SQLite）")
    parser.add_argument("--seed", type=int, default=SEED, help=f"随机种子（默认 {SEED}，固定以保证可复现）")
    args = parser.parse_args(argv)

    summary = init_demo_data(seed=args.seed)
    print("演示数据生成完成：")
    print(f"  stores    {summary['stores']:>6} 行")
    print(f"  products  {summary['products']:>6} 行")
    print(f"  customers {summary['customers']:>6} 行（含敏感字段 name/phone）")
    print(f"  sales     {summary['sales']:>6} 行（2025-01-01 ~ 2025-12-31，Q4 高季节性）")
    print(f"  CSV 目录  {summary['raw_dir']}")
    print(f"  SQLite    {summary['db_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
