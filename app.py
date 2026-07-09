import os
import re
from datetime import datetime, timezone
from bson import ObjectId
from flask import Flask, request, jsonify, render_template
from pymongo import MongoClient, DESCENDING
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ── MongoDB ───────────────────────────────────────────────
MONGO_URI   = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB    = os.getenv("MONGO_DB",  "saravana_billing")
ACCOUNTS_DB = os.getenv("ACCOUNTS_DB", "home_appliances_db")

client = MongoClient(MONGO_URI)

db       = client[MONGO_DB]
invoices = db["invoices"]

adb       = client[ACCOUNTS_DB]
products  = adb["products"]
suppliers = adb["suppliers"]
purchases = adb["purchases"]
sales_col = adb["sales"]


# ════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════

def serialize(doc):
    """Convert MongoDB document to JSON-serializable dict."""
    doc["_id"] = str(doc["_id"])
    for k, v in list(doc.items()):
        if isinstance(v, ObjectId):
            doc[k] = str(v)
        elif isinstance(v, datetime):
            doc[k] = v.isoformat()
    return doc


def oid(id_str):
    """Safely parse ObjectId."""
    try:
        return ObjectId(id_str)
    except Exception:
        raise ValueError("Invalid ID")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ── Stock helpers ─────────────────────────────────────────

def adjust_stock_by_name(name: str, delta: int):
    """
    Adjust stock for a product matched by name (case-insensitive).
    delta > 0 = add stock (purchase), delta < 0 = remove stock (sale).
    """
    if not name or delta == 0:
        return
    products.update_one(
        {"name": {"$regex": f"^{re.escape(name.strip())}$", "$options": "i"}},
        {"$inc": {"stock": delta}}
    )


def adjust_stock_by_id(prod_id: str, delta: int):
    """Adjust stock for a product matched by its ObjectId string."""
    if not prod_id or delta == 0:
        return
    try:
        products.update_one(
            {"_id": ObjectId(prod_id)},
            {"$inc": {"stock": delta}}
        )
    except Exception:
        pass


def adjust_stock_by_name(name: str, delta: int, buy_price: float = 0, category: str = "Uncategorized"):
    """
    Adjust stock for a product matched by name (case-insensitive).
    If it doesn't exist and delta > 0, create it automatically.
    """
    if not name or delta == 0:
        return
    
    existing = products.find_one({"name": {"$regex": f"^{re.escape(name.strip())}$", "$options": "i"}})
    if existing:
        products.update_one({"_id": existing["_id"]}, {"$inc": {"stock": delta}})
    else:
        if delta > 0:
            # Auto-generate product code based on category
            cat_clean = category.strip() if category and category.strip() else "Uncategorized"
            prefix = cat_clean[:3].upper() if cat_clean != "Uncategorized" else "GEN"
            
            # Find count to generate sequential ID
            count = products.count_documents({"product_code": {"$regex": f"^{re.escape(prefix)}-"}})
            new_code = f"{prefix}-{count + 1:03d}"
            
            # Auto-create the product from purchase
            products.insert_one({
                "product_code": new_code,
                "name": name.strip(),
                "stock": delta,
                "buy_price": buy_price,
                "sell_price": buy_price,
                "category": cat_clean,
                "gst": 18
            })


def adjust_stock_by_code(code: str, delta: int):
    """Adjust stock by product_code."""
    if not code or delta == 0:
        return
    products.update_one(
        {"product_code": {"$regex": f"^{re.escape(code.strip())}$", "$options": "i"}},
        {"$inc": {"stock": delta}}
    )


# ── Supplier ledger helpers ───────────────────────────────

def adjust_supplier_ledger(supplier_name: str, amount_delta: float, date_str: str = None):
    """
    Update supplier's running total and last purchase date.
    amount_delta > 0 = purchase added, < 0 = purchase removed/corrected.
    """
    if not supplier_name:
        return
    update = {"$inc": {"total_purchased": amount_delta}}
    if date_str and amount_delta > 0:
        update["$set"] = {"last_purchase_date": date_str}
    suppliers.update_one(
        {"supplier_name": {"$regex": f"^{re.escape(supplier_name.strip())}$", "$options": "i"}},
        update
    )


# ── Invoice stock helpers ─────────────────────────────────

def _apply_invoice_items(item_list, sign: int):
    """
    Apply stock change for a list of invoice items.
    sign = -1 for sale (deduct), +1 for reversal (restore).
    """
    for item in (item_list or []):
        name = item.get("name", "").strip()
        qty  = int(float(item.get("qty", 0)))
        if name and qty > 0:
            adjust_stock_by_name(name, sign * qty)


# ── Purchase stock helpers ────────────────────────────────

def _apply_purchase(purchase_doc, sign: int):
    """
    Apply stock change for a purchase document.
    sign = +1 for new purchase (add stock), -1 for reversal (remove stock).
    """
    pid  = purchase_doc.get("product_id")
    pname = purchase_doc.get("product_name", "").strip()
    code = purchase_doc.get("product_code", "").strip()
    category = purchase_doc.get("category", "Uncategorized").strip()
    qty  = int(float(purchase_doc.get("quantity", 0)))
    price = float(purchase_doc.get("price", purchase_doc.get("buy_price", 0)))
    if qty > 0:
        if pid:
            adjust_stock_by_id(pid, sign * qty)
        elif pname:
            adjust_stock_by_name(pname, sign * qty, price if sign > 0 else 0, category)
        elif code:
            adjust_stock_by_code(code, sign * qty)


# ════════════════════════════════════════════════════════
#  PAGE ROUTES
# ════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("invoice.html")


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


# ════════════════════════════════════════════════════════
#  STATS API
# ════════════════════════════════════════════════════════

@app.route("/api/stats", methods=["GET"])
def get_stats():
    """Combined dashboard statistics — single call for all top-level numbers."""
    now = datetime.now(timezone.utc)

    # Invoice stats
    all_inv   = list(invoices.find({}, {"net_amount": 1, "invoice_date": 1, "items": 1}))
    total_inv = len(all_inv)
    total_rev = sum(i.get("net_amount", 0) for i in all_inv)
    month_rev = sum(
        i.get("net_amount", 0) for i in all_inv
        if i.get("invoice_date", "")[:7] == now.strftime("%Y-%m")
    )
    avg_inv   = total_rev / total_inv if total_inv else 0

    # Stock stats
    all_prods    = list(products.find({}, {"stock": 1}))
    total_prods  = len(all_prods)
    low_stock    = sum(1 for p in all_prods if 0 < (p.get("stock") or 0) <= 5)
    out_of_stock = sum(1 for p in all_prods if (p.get("stock") or 0) <= 0)

    # Purchase stats
    all_purch    = list(purchases.find({}, {"total": 1, "purchase_date": 1}))
    total_purch  = sum(p.get("total", 0) for p in all_purch)
    month_purch  = sum(
        p.get("total", 0) for p in all_purch
        if str(p.get("purchase_date", ""))[:7] == now.strftime("%Y-%m")
    )

    return jsonify({
        "invoices": {
            "total": total_inv,
            "revenue": total_rev,
            "month_revenue": month_rev,
            "avg": avg_inv,
        },
        "stock": {
            "total_products": total_prods,
            "low_stock": low_stock,
            "out_of_stock": out_of_stock,
        },
        "purchases": {
            "total_value": total_purch,
            "month_value": month_purch,
        },
        "month_label": now.strftime("%B %Y"),
    })


# ════════════════════════════════════════════════════════
#  INVOICE API
# ════════════════════════════════════════════════════════

@app.route("/api/invoices", methods=["POST"])
def save_invoice():
    """
    Save a new invoice.
    ✅ Auto-deducts stock for every line item by product name.
    """
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "No data provided"}), 400

    data["saved_at"] = now_iso()
    result = invoices.insert_one(data)

    # Deduct stock
    _apply_invoice_items(data.get("items", []), sign=-1)

    return jsonify({"success": True, "id": str(result.inserted_id)}), 201


@app.route("/api/invoices", methods=["GET"])
def list_invoices():
    """Return all invoices, newest first. Supports ?q= search."""
    q = request.args.get("q", "").strip()
    query = {}
    if q:
        query = {
            "$or": [
                {"customer_name": {"$regex": q, "$options": "i"}},
                {"invoice_no":    {"$regex": q, "$options": "i"}},
            ]
        }
    docs = list(invoices.find(query).sort("saved_at", DESCENDING))
    return jsonify([serialize(d) for d in docs])


@app.route("/api/invoices/next-number", methods=["GET"])
def next_invoice_number():
    """Auto-suggest next invoice number based on last saved."""
    last = invoices.find_one({}, sort=[("saved_at", DESCENDING)])
    if not last or not last.get("invoice_no"):
        return jsonify({"next": "INV-001"})

    inv_no = last["invoice_no"].strip()
    match  = re.search(r"(\d+)$", inv_no)
    if match:
        num     = int(match.group(1))
        prefix  = inv_no[: match.start()]
        width   = len(match.group(1))
        next_no = f"{prefix}{str(num + 1).zfill(width)}"
    else:
        next_no = inv_no + "-2"

    return jsonify({"next": next_no})


@app.route("/api/invoices/<invoice_id>", methods=["GET"])
def get_invoice(invoice_id):
    try:
        doc = invoices.find_one({"_id": oid(invoice_id)})
    except ValueError:
        return jsonify({"error": "Invalid ID"}), 400
    if not doc:
        return jsonify({"error": "Not found"}), 404
    return jsonify(serialize(doc))


@app.route("/api/invoices/<invoice_id>", methods=["PUT"])
def update_invoice(invoice_id):
    """
    Update invoice.
    ✅ Tally sync: restores old item stock → applies new item stock.
    """
    try:
        _id = oid(invoice_id)
    except ValueError:
        return jsonify({"error": "Invalid ID"}), 400

    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "No data provided"}), 400

    # Fetch existing invoice to reverse its stock effect
    old = invoices.find_one({"_id": _id})
    if not old:
        return jsonify({"error": "Not found"}), 404

    # Restore stock from old items
    _apply_invoice_items(old.get("items", []), sign=+1)

    # Apply stock from new items
    _apply_invoice_items(data.get("items", old.get("items", [])), sign=-1)

    data.pop("_id", None)
    data["updated_at"] = now_iso()
    invoices.update_one({"_id": _id}, {"$set": data})

    return jsonify({"success": True})


@app.route("/api/invoices/<invoice_id>", methods=["DELETE"])
def delete_invoice(invoice_id):
    """
    Delete invoice.
    ✅ Tally sync: restores stock for all line items.
    """
    try:
        _id = oid(invoice_id)
    except ValueError:
        return jsonify({"error": "Invalid ID"}), 400

    doc = invoices.find_one({"_id": _id})
    if not doc:
        return jsonify({"error": "Not found"}), 404

    # Restore stock before deleting
    _apply_invoice_items(doc.get("items", []), sign=+1)

    invoices.delete_one({"_id": _id})
    return jsonify({"success": True})


# ════════════════════════════════════════════════════════
#  PRODUCTS API
# ════════════════════════════════════════════════════════

@app.route("/api/products/search", methods=["GET"])
def product_search():
    """Autosuggest — up to 10 products matching ?q="""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])

    docs = list(products.find(
        {"name": {"$regex": q, "$options": "i"}},
        {"name": 1, "product_code": 1, "category": 1,
         "sell_price": 1, "buy_price": 1, "stock": 1, "gst": 1}
    ).limit(10))

    return jsonify([{
        "id":           str(d["_id"]),
        "name":         d.get("name", ""),
        "product_code": d.get("product_code", ""),
        "category":     d.get("category", ""),
        "sell_price":   d.get("sell_price", 0),
        "buy_price":    d.get("buy_price", 0),
        "stock":        d.get("stock", 0),
        "gst":          d.get("gst", 18),
    } for d in docs])


@app.route("/api/products", methods=["GET"])
def list_products():
    """
    Return all products enriched with:
    - total_sold   (computed from invoices)
    - total_bought (computed from purchases)
    """
    all_prods = list(products.find({}).sort("category", 1))

    # Build total_sold map from invoices (by product name)
    sold_map = {}
    for inv in invoices.find({}, {"items": 1}):
        for item in (inv.get("items") or []):
            name = (item.get("name") or "").strip().lower()
            qty  = int(float(item.get("qty", 0)))
            sold_map[name] = sold_map.get(name, 0) + qty

    # Build total_bought map from purchases (by product_id or name/code)
    bought_map_by_id = {}
    bought_map_by_name = {}
    for p in purchases.find({}, {"product_id": 1, "product_name": 1, "product_code": 1, "quantity": 1}):
        qty = int(float(p.get("quantity", 0)))
        pid = str(p.get("product_id") or "")
        pname = (p.get("product_name") or "").strip().lower()
        pcode = (p.get("product_code") or "").strip().lower()
        if pid:
            bought_map_by_id[pid] = bought_map_by_id.get(pid, 0) + qty
        if pname:
            bought_map_by_name[pname] = bought_map_by_name.get(pname, 0) + qty
        if pcode:
            bought_map_by_name[pcode] = bought_map_by_name.get(pcode, 0) + qty

    result = []
    for d in all_prods:
        s = serialize(d)
        pid = str(s.get("_id"))
        name = (s.get("name") or "").strip().lower()
        code = (s.get("product_code") or "").strip().lower()
        s["total_sold"]   = sold_map.get(name, 0)
        s["total_bought"] = bought_map_by_id.get(pid) or bought_map_by_name.get(name) or bought_map_by_name.get(code) or 0
        result.append(s)

    return jsonify(result)


@app.route("/api/products", methods=["POST"])
def create_product():
    data = request.get_json(force=True)
    if not data or not data.get("name"):
        return jsonify({"error": "Product name is required"}), 400
    data["created_at"] = now_iso()
    result = products.insert_one(data)
    return jsonify({"success": True, "id": str(result.inserted_id)}), 201


@app.route("/api/products/<product_id>", methods=["PUT"])
def update_product(product_id):
    try:
        _id = oid(product_id)
    except ValueError:
        return jsonify({"error": "Invalid ID"}), 400
    data = request.get_json(force=True)
    data.pop("_id", None)
    result = products.update_one({"_id": _id}, {"$set": data})
    if result.matched_count == 0:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"success": True})


@app.route("/api/products/<product_id>", methods=["DELETE"])
def delete_product(product_id):
    try:
        result = products.delete_one({"_id": oid(product_id)})
    except ValueError:
        return jsonify({"error": "Invalid ID"}), 400
    if result.deleted_count == 0:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"success": True})


# ════════════════════════════════════════════════════════
#  SUPPLIERS API
# ════════════════════════════════════════════════════════

@app.route("/api/suppliers", methods=["GET"])
def list_suppliers():
    """
    Return suppliers enriched with:
    - total_purchased (computed from purchases)
    - last_purchase_date (latest purchase date for this supplier)
    """
    all_sup = list(suppliers.find({}).sort("supplier_name", 1))

    # Compute per-supplier purchase totals from purchases collection
    purch_map = {}  # supplier_name.lower() -> {total, last_date}
    for p in purchases.find({}, {"supplier_name": 1, "total": 1, "purchase_date": 1}):
        sname = (p.get("supplier_name") or "").strip().lower()
        if not sname:
            continue
        if sname not in purch_map:
            purch_map[sname] = {"total": 0, "last_date": ""}
        purch_map[sname]["total"] += float(p.get("total", 0))
        d = str(p.get("purchase_date", ""))
        if d > purch_map[sname]["last_date"]:
            purch_map[sname]["last_date"] = d

    result = []
    for s in all_sup:
        doc = serialize(s)
        key = (doc.get("supplier_name") or "").strip().lower()
        pm  = purch_map.get(key, {})
        doc["computed_total_purchased"] = pm.get("total", 0)
        doc["computed_last_purchase"]   = pm.get("last_date", "")
        result.append(doc)

    return jsonify(result)


@app.route("/api/suppliers", methods=["POST"])
def create_supplier():
    data = request.get_json(force=True)
    if not data or not data.get("supplier_name"):
        return jsonify({"error": "Supplier name is required"}), 400
    data["created_at"] = now_iso()
    result = suppliers.insert_one(data)
    return jsonify({"success": True, "id": str(result.inserted_id)}), 201


@app.route("/api/suppliers/<supplier_id>", methods=["PUT"])
def update_supplier(supplier_id):
    try:
        _id = oid(supplier_id)
    except ValueError:
        return jsonify({"error": "Invalid ID"}), 400
    data = request.get_json(force=True)
    data.pop("_id", None)
    result = suppliers.update_one({"_id": _id}, {"$set": data})
    if result.matched_count == 0:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"success": True})


@app.route("/api/suppliers/<supplier_id>", methods=["DELETE"])
def delete_supplier(supplier_id):
    try:
        result = suppliers.delete_one({"_id": oid(supplier_id)})
    except ValueError:
        return jsonify({"error": "Invalid ID"}), 400
    if result.deleted_count == 0:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"success": True})


# ════════════════════════════════════════════════════════
#  PURCHASES API
# ════════════════════════════════════════════════════════

@app.route("/api/purchases", methods=["GET"])
def list_purchases():
    """Return all purchases newest first, with supplier name resolved."""
    docs = list(purchases.find({}).sort("purchase_date", DESCENDING))

    sup_map = {str(s["_id"]): s.get("supplier_name", "—")
               for s in suppliers.find({}, {"supplier_name": 1})}

    result = []
    for d in docs:
        d["supplier_name"] = sup_map.get(
            str(d.get("supplier_id", "")),
            d.get("supplier_name", "—")
        )
        result.append(serialize(d))
    return jsonify(result)


@app.route("/api/purchases", methods=["POST"])
def create_purchase():
    """
    Add a purchase.
    ✅ Tally sync:
       - stock += quantity  (product_code lookup)
       - supplier total_purchased += total
    """
    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "No data provided"}), 400

    if not data.get("pur_id") and not data.get("purchase_no"):
        count = purchases.count_documents({})
        new_id = f"PUR-{count + 1:04d}"
        data["pur_id"] = new_id
        data["purchase_no"] = new_id

    data["created_at"] = now_iso()
    result = purchases.insert_one(data)

    # Adjust stock
    _apply_purchase(data, sign=+1)

    # Adjust supplier ledger
    adjust_supplier_ledger(
        data.get("supplier_name", ""),
        float(data.get("total", 0)),
        data.get("purchase_date", "")
    )

    return jsonify({"success": True, "id": str(result.inserted_id)}), 201


@app.route("/api/purchases/<purchase_id>", methods=["PUT"])
def update_purchase(purchase_id):
    """
    Update a purchase.
    ✅ Tally sync:
       - Reverses old stock effect, applies new stock effect (delta).
       - Reverses old supplier amount, applies new supplier amount.
    """
    try:
        _id = oid(purchase_id)
    except ValueError:
        return jsonify({"error": "Invalid ID"}), 400

    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "No data provided"}), 400

    old = purchases.find_one({"_id": _id})
    if not old:
        return jsonify({"error": "Not found"}), 404

    # Reverse old stock
    _apply_purchase(old, sign=-1)
    # Apply new stock
    _apply_purchase(data, sign=+1)

    # Reverse old supplier total
    adjust_supplier_ledger(
        old.get("supplier_name", ""),
        -float(old.get("total", 0))
    )
    # Apply new supplier total
    adjust_supplier_ledger(
        data.get("supplier_name", ""),
        float(data.get("total", 0)),
        data.get("purchase_date", "")
    )

    data.pop("_id", None)
    data["updated_at"] = now_iso()
    purchases.update_one({"_id": _id}, {"$set": data})

    return jsonify({"success": True})


@app.route("/api/purchases/<purchase_id>", methods=["DELETE"])
def delete_purchase(purchase_id):
    """
    Delete a purchase.
    ✅ Tally sync:
       - stock -= quantity (reverses the purchase).
       - supplier total_purchased -= total.
    """
    try:
        _id = oid(purchase_id)
    except ValueError:
        return jsonify({"error": "Invalid ID"}), 400

    doc = purchases.find_one({"_id": _id})
    if not doc:
        return jsonify({"error": "Not found"}), 404

    # Reverse stock
    _apply_purchase(doc, sign=-1)

    # Reverse supplier ledger
    adjust_supplier_ledger(
        doc.get("supplier_name", ""),
        -float(doc.get("total", 0))
    )

    purchases.delete_one({"_id": _id})
    return jsonify({"success": True})


# ════════════════════════════════════════════════════════
#  SALES API (legacy — kept for backward compat)
# ════════════════════════════════════════════════════════

@app.route("/api/sales", methods=["GET"])
def list_sales():
    docs = list(sales_col.find({}).sort("sale_date", DESCENDING))
    return jsonify([serialize(d) for d in docs])


# ── Run ───────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True, port=5000)
