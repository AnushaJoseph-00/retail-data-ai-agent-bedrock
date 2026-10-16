"""Single source of truth for the retail data warehouse schema.

Used by setup_athena.py (to create the Athena tables) and agent.py
(to tell the model what tables exist and how they join).
"""

TABLES = {
    "customers": {
        "columns": [("customer_id", "int"), ("city", "string"), ("signup_date", "date")],
        "about": "One row per customer.",
    },
    "stores": {
        "columns": [("store_id", "int"), ("city", "string")],
        "about": "One row per physical store.",
    },
    "employees": {
        "columns": [("employee_id", "int"), ("store_id", "int"), ("salary", "int")],
        "about": "One row per employee. store_id -> stores.",
    },
    "categories": {
        "columns": [("category_id", "int"), ("category_name", "string")],
        "about": "Product categories.",
    },
    "suppliers": {
        "columns": [("supplier_id", "int"), ("country", "string")],
        "about": "One row per supplier.",
    },
    "products": {
        "columns": [("product_id", "int"), ("category_id", "int"), ("supplier_id", "int"), ("price", "int")],
        "about": "List price per product. category_id -> categories, supplier_id -> suppliers.",
    },
    "promotions": {
        "columns": [("promotion_id", "int"), ("discount", "int")],
        "about": "Discount is a percentage.",
    },
    "orders": {
        "columns": [("order_id", "int"), ("customer_id", "int"), ("store_id", "int"),
                    ("order_date", "date"), ("promotion_id", "int")],
        "about": "One row per order. customer_id -> customers, store_id -> stores, promotion_id -> promotions.",
    },
    "order_items": {
        "columns": [("order_item_id", "int"), ("order_id", "int"), ("product_id", "int"),
                    ("qty", "int"), ("price", "int")],
        "about": "One row per product line in an order. Line revenue = qty * price. "
                 "order_id -> orders, product_id -> products.",
    },
    "payments": {
        "columns": [("payment_id", "int"), ("order_id", "int"), ("amount", "int")],
        "about": "Amount paid for an order. order_id -> orders.",
    },
    "shipments": {
        "columns": [("shipment_id", "int"), ("order_id", "int"), ("status", "string")],
        "about": "Delivery status per order: 'delivered', 'shipped' or 'late'. order_id -> orders.",
    },
    "returns": {
        "columns": [("return_id", "int"), ("order_item_id", "int"), ("refund", "int")],
        "about": "Refunds for returned order lines. order_item_id -> order_items.",
    },
}


def schema_text():
    """Compact description of every table for the model's system prompt."""
    lines = []
    for name, t in TABLES.items():
        cols = ", ".join(f"{c} {ty}" for c, ty in t["columns"])
        lines.append(f"- {name}({cols}): {t['about']}")
    return "\n".join(lines)
