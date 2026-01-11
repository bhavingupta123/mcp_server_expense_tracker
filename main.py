from fastmcp import FastMCP
import os
import aiosqlite  # Changed: sqlite3 → aiosqlite
import tempfile
import motor.motor_asyncio

# Try to use a persistent path, fallback to temp if not writable
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CATEGORIES_PATH = os.path.join(PROJECT_DIR, "categories.json")

# Check if we can write to the project directory
def get_db_path():
    project_db = os.path.join(PROJECT_DIR, "expenses.db")
    try:
        # Try to create/open in project directory
        with open(project_db, 'a'):
            pass
        return project_db
    except (PermissionError, OSError):
        # Fallback to temp directory (note: data won't persist across restarts)
        print("WARNING: Using temp directory for database - data will not persist!")
        return os.path.join(tempfile.gettempdir(), "expenses.db")

DB_PATH = get_db_path()

print(f"Database path: {DB_PATH}")

mcp = FastMCP("ExpenseTracker")

# MongoDB Atlas connection
MONGO_URI = "mongodb+srv://guptabhavin60_db_user:ce1wZlthNh7Az70u@cluster0.udf3awx.mongodb.net/"
MONGO_DB = "Expense"
MONGO_COLLECTION = "ExpenseCollection"

mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
mongo_db = mongo_client[MONGO_DB]
mongo_expenses = mongo_db[MONGO_COLLECTION]

def init_db():  # Keep as sync for initialization
    try:
        # Use synchronous sqlite3 just for initialization
        import sqlite3
        with sqlite3.connect(DB_PATH) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("""
                CREATE TABLE IF NOT EXISTS expenses(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    amount REAL NOT NULL,
                    category TEXT NOT NULL,
                    subcategory TEXT DEFAULT '',
                    note TEXT DEFAULT ''
                )
            """)
            # Test write access
            c.execute("INSERT OR IGNORE INTO expenses(date, amount, category) VALUES ('2000-01-01', 0, 'test')")
            c.execute("DELETE FROM expenses WHERE category = 'test'")
            print("Database initialized successfully with write access")
    except Exception as e:
        print(f"Database initialization error: {e}")
        raise

# Initialize database synchronously at module load
init_db()

@mcp.tool()
async def add_expense(date, amount, category, subcategory="", note=""):
    '''Add a new expense entry to MongoDB.'''
    doc = {
        "date": date,
        "amount": amount,
        "category": category,
        "subcategory": subcategory,
        "note": note
    }
    result = await mongo_expenses.insert_one(doc)
    doc["_id"] = str(result.inserted_id)
    return doc

@mcp.tool()
async def list_expenses(startDate=None, endDate=None):
    '''List expenses from MongoDB, optionally filtered by date.'''
    query = {}
    if startDate and endDate:
        query["date"] = {"$gte": startDate, "$lte": endDate}
    cursor = mongo_expenses.find(query)
    expenses = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        expenses.append(doc)
    return expenses

@mcp.tool()
async def delete_expense(expense_id):
    '''Delete an expense from MongoDB by _id.'''
    from bson import ObjectId
    result = await mongo_expenses.delete_one({"_id": ObjectId(expense_id)})
    return {"deleted": result.deleted_count}

@mcp.tool()
async def update_expense(expense_id, category, subcategory, note):
    '''Update an expense in MongoDB by _id.'''
    from bson import ObjectId
    result = await mongo_expenses.update_one(
        {"_id": ObjectId(expense_id)},
        {"$set": {"category": category, "subcategory": subcategory, "note": note}}
    )
    return {"updated": result.modified_count}

@mcp.tool()
async def summarize(start_date, end_date, category=None):  # Changed: added async
    '''Summarize expenses by category within an inclusive date range.'''
    try:
        async with aiosqlite.connect(DB_PATH) as c:  # Changed: added async
            query = """
                SELECT category, SUM(amount) AS total_amount, COUNT(*) as count
                FROM expenses
                WHERE date BETWEEN ? AND ?
            """
            params = [start_date, end_date]

            if category:
                query += " AND category = ?"
                params.append(category)

            query += " GROUP BY category ORDER BY total_amount DESC"

            cur = await c.execute(query, params)  # Changed: added await
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in await cur.fetchall()]  # Changed: added await
    except Exception as e:
        return {"status": "error", "message": f"Error summarizing expenses: {str(e)}"}

@mcp.tool()
async def parse_transaction(text: str, sender: str = None):
    '''Parse a free-form SMS/email/notification text to extract amount, date, merchant, note.
    Also perform simple sender-based bank detection and return `is_bank` and `confidence`.
    '''
    try:
        import re
        from datetime import datetime

        t = text or ""
        s = (sender or "").strip()
        # Try GPay style: "paid ₹123.45 to ABC Store on 10 Jan 2026"
        m = re.search(r"paid [₹Rs.]*([0-9,]+(?:\.[0-9]+)?) to ([\w &.\-]+) on ([0-9]{1,2} [A-Za-z]{3,} [0-9]{4})", t, re.IGNORECASE)
        if m:
            amount = float(m.group(1).replace(',', ''))
            merchant = m.group(2).strip()
            try:
                dt = datetime.strptime(m.group(3), "%d %b %Y").date().isoformat()
            except Exception:
                dt = datetime.utcnow().date().isoformat()
            # heuristics: likely bank/payment
            is_bank = True
            confidence = 0.9
            # suggest category based on merchant heuristics
            suggested_category = "Other"
            mk = merchant.lower()
            if any(k in mk for k in ["uber", "ola", "taxi", "cab", "fuel", "petrol"]).__bool__():
                suggested_category = "Transportation"
            elif any(k in mk for k in ["restaurant", "cafe", "dine", "bar", "hotel"]).__bool__():
                suggested_category = "Food & Dining"
            elif any(k in mk for k in ["flipkart", "amazon", "myntra", "shop", "store", "super", "grocery", "grocer"]).__bool__():
                suggested_category = "Shopping"
            return {"status": "success", "amount": amount, "date": dt, "merchant": merchant, "note": t, "is_bank": is_bank, "confidence": confidence, "suggested_category": suggested_category}

        # Bank SMS: "debited for Rs.1.00 on 11-01-26 trf to SANDEEP GUPTA"
        m = re.search(r"debited for [₹Rs.]*([0-9,]+(?:\.[0-9]+)?) on ([0-9]{2}-[0-9]{2}-[0-9]{2,4})(?: .*to ([\w &.\-]+))?", t, re.IGNORECASE)
        if m:
            amount = float(m.group(1).replace(',', ''))
            raw_date = m.group(2)
            merchant = (m.group(3) or "").strip()
            # parse date formats dd-mm-yy or dd-mm-yyyy
            parsed_date = None
            for fmt in ("%d-%m-%Y", "%d-%m-%y"):
                try:
                    parsed_date = datetime.strptime(raw_date, fmt).date().isoformat()
                    break
                except Exception:
                    continue
            if parsed_date is None:
                parsed_date = datetime.utcnow().date().isoformat()
            # sender-based detection
            is_bank = False
            confidence = 0.6
            if s:
                su = s.upper()
                bank_keywords = ["KBL", "KARNATAKA", "SBI", "HDFC", "ICICI", "AXIS", "PNB", "YESBANK", "IDFC", "KOTAK", "CANARA", "BANK", "BNK", "PAYTM", "PHONEPE", "GOOGLEPAY", "GPAISA", "NBUPAISA"]
                if any(k in su for k in bank_keywords):
                    is_bank = True
                    confidence = 0.95
                # alphanumeric sender (like KBLBNK) is usually a bank
                elif re.match(r"^[A-Z]{3,15}$", su):
                    is_bank = True
                    confidence = 0.9
                # short numeric sender (shortcodes) also often banks
                elif re.match(r"^[0-9]{3,6}$", su):
                    is_bank = True
                    confidence = 0.8
            # suggest category from merchant or default to Bills/Other
            suggested_category = "Other"
            mk = (merchant or "").lower()
            if any(k in mk for k in ["upi", "wallet", "phonepe", "paytm", "gpay"]):
                suggested_category = "Payment"
            elif any(k in mk for k in ["atm", "bank", "karnataka", "kbl", "sbi", "hdfc", "icici"]):
                suggested_category = "Bills & Utilities"
            return {"status": "success", "amount": amount, "date": parsed_date, "merchant": merchant or "Bank", "note": t, "is_bank": is_bank, "confidence": confidence, "suggested_category": suggested_category}

        # Email style: "Account ... has been DEBITED for Rs.1.00"
        m = re.search(r"DEBITED for [₹Rs.]*([0-9,]+(?:\.[0-9]+)?)", t, re.IGNORECASE)
        if m:
            amount = float(m.group(1).replace(',', ''))
            is_bank = False
            confidence = 0.6
            if s:
                su = s.upper()
                if any(k in su for k in ["BANK", "BNK", "KBL", "SBI", "HDFC", "ICICI"]):
                    is_bank = True
                    confidence = 0.9
            suggested_category = "Bills & Utilities"
            return {"status": "success", "amount": amount, "date": datetime.utcnow().date().isoformat(), "merchant": "Bank", "note": t, "is_bank": is_bank, "confidence": confidence, "suggested_category": suggested_category}

        # Fallback: look for just an amount
        m = re.search(r"[₹Rs.]*([0-9,]+(?:\.[0-9]+)?)", t)
        if m:
            amount = float(m.group(1).replace(',', ''))
            # best-effort fallback
            is_bank = False
            confidence = 0.3
            if s:
                su = s.upper()
                if any(k in su for k in ["BANK", "BNK", "KBL", "SBI", "HDFC", "ICICI"]):
                    is_bank = True
                    confidence = 0.8
            suggested_category = "Other"
            return {"status": "success", "amount": amount, "date": datetime.utcnow().date().isoformat(), "merchant": "Unknown", "note": t, "is_bank": is_bank, "confidence": confidence, "suggested_category": suggested_category}

        return {"status": "error", "message": "Could not parse transaction"}
    except Exception as e:
        return {"status": "error", "message": f"Parser error: {str(e)}"}

@mcp.resource("expense:///categories", mime_type="application/json")  # Changed: expense:// → expense:///
def categories():
    try:
        # Provide default categories if file doesn't exist
        default_categories = {
            "categories": [
                "Food & Dining",
                "Transportation",
                "Shopping",
                "Entertainment",
                "Bills & Utilities",
                "Healthcare",
                "Travel",
                "Education",
                "Business",
                "Other"
            ]
        }
        
        try:
            with open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            import json
            return json.dumps(default_categories, indent=2)
    except Exception as e:
        return f'{{"error": "Could not load categories: {str(e)}"}}'


@mcp.tool()
async def update_expense(expense_id: int, category: str, subcategory: str = "", note: str = ""):
    '''Update category/subcategory/note for an expense by id.'''
    try:
        from bson import ObjectId
        result = await mongo_expenses.update_one(
            {"_id": ObjectId(expense_id)},
            {"$set": {"category": category, "subcategory": subcategory, "note": note}}
        )
        return {"updated": result.modified_count}
    except Exception as e:
        return {"status": "error", "message": f"Error updating expense: {str(e)}"}


@mcp.tool()
async def categorize_transaction(text: str, sender: str = None):
    '''Return a suggested category for a free-form text using existing parser heuristics or model.'''
    try:
        # Reuse parse_transaction heuristics and return suggested_category
        parsed = await parse_transaction(text, sender)
        if isinstance(parsed, dict) and parsed.get("status") == "success":
            return {"status": "success", "suggested_category": parsed.get("suggested_category", "Other"), "confidence": parsed.get("confidence", 0.0)}
        return {"status": "error", "message": "Could not categorize"}
    except Exception as e:
        return {"status": "error", "message": f"Categorize error: {str(e)}"}

# Start the server
if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)
    # mcp.run()