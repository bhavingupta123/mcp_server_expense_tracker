from fastmcp import FastMCP
import os
import aiosqlite
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

# UserPassword collection
mongo_users = mongo_client["Expense"]["UserPassword"]

def init_db():
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


# ============== USER AUTHENTICATION ==============

@mcp.tool()
async def register_user(phone: str, password: str):
    '''Register a new user with phone and password.'''
    try:
        # Validate input
        if not phone or len(phone.strip()) < 10:
            return {"status": "error", "message": "Please enter a valid phone number (at least 10 digits)."}
        
        if not password or len(password.strip()) < 4:
            return {"status": "error", "message": "Password must be at least 4 characters."}
        
        phone = phone.strip()
        password = password.strip()
        
        # Check if already registered
        existing = await mongo_users.find_one({"phone": phone})
        if existing:
            return {"status": "error", "message": "Phone number already registered. Please login."}
        
        # Create new user
        doc = {"phone": phone, "password": password}
        result = await mongo_users.insert_one(doc)
        return {"status": "success", "user_id": str(result.inserted_id), "message": "Registration successful!"}
    except Exception as e:
        return {"status": "error", "message": f"Registration failed: {str(e)}"}


@mcp.tool()
async def login_user(phone: str, password: str):
    '''Authenticate user by phone and password.'''
    try:
        if not phone or not password:
            return {"status": "error", "message": "Please enter phone number and password."}
        
        phone = phone.strip()
        password = password.strip()
        
        user = await mongo_users.find_one({"phone": phone, "password": password})
        if user:
            return {"status": "success", "user_id": str(user["_id"]), "message": "Login successful!"}
        else:
            # Check if user exists but password is wrong
            user_exists = await mongo_users.find_one({"phone": phone})
            if user_exists:
                return {"status": "error", "message": "Incorrect password. Please try again."}
            else:
                return {"status": "error", "message": "Phone number not registered. Please create an account."}
    except Exception as e:
        return {"status": "error", "message": f"Login failed: {str(e)}"}


# ============== EXPENSE MANAGEMENT ==============

@mcp.tool()
async def add_expense(phone: str, date: str, amount: float, category: str, subcategory: str = "", note: str = ""):
    '''Add a new expense entry to MongoDB for a user.'''
    try:
        if not phone:
            return {"status": "error", "message": "Phone number is required."}
        
        doc = {
            "phone": phone.strip(),
            "date": date,
            "amount": float(amount),
            "category": category,
            "subcategory": subcategory or "",
            "note": note or ""
        }
        result = await mongo_expenses.insert_one(doc)
        return {"status": "success", "id": str(result.inserted_id), "message": "Expense added successfully!"}
    except Exception as e:
        return {"status": "error", "message": f"Failed to add expense: {str(e)}"}


@mcp.tool()
async def list_expenses(phone: str, start_date: str = None, end_date: str = None):
    '''List expenses for a user from MongoDB, optionally filtered by date.'''
    try:
        if not phone:
            return []
        
        query = {"phone": phone.strip()}
        if start_date and end_date:
            query["date"] = {"$gte": start_date, "$lte": end_date}
        
        cursor = mongo_expenses.find(query).sort("date", -1)  # Sort by date descending
        expenses = []
        async for doc in cursor:
            expenses.append({
                "id": str(doc["_id"]),
                "phone": doc.get("phone", ""),
                "date": doc.get("date", ""),
                "amount": doc.get("amount", 0),
                "category": doc.get("category", ""),
                "subcategory": doc.get("subcategory", ""),
                "note": doc.get("note", "")
            })
        return expenses
    except Exception as e:
        return {"status": "error", "message": f"Failed to list expenses: {str(e)}"}


@mcp.tool()
async def delete_expense(expense_id: str, phone: str):
    '''Delete an expense from MongoDB by _id, verifying ownership by phone.'''
    try:
        from bson import ObjectId
        
        if not expense_id or not phone:
            return {"status": "error", "message": "Expense ID and phone are required."}
        
        # Verify ownership before deleting
        expense = await mongo_expenses.find_one({"_id": ObjectId(expense_id)})
        if not expense:
            return {"status": "error", "message": "Expense not found."}
        
        if expense.get("phone") != phone.strip():
            return {"status": "error", "message": "You can only delete your own expenses."}
        
        result = await mongo_expenses.delete_one({"_id": ObjectId(expense_id)})
        if result.deleted_count > 0:
            return {"status": "success", "deleted": 1, "message": "Expense deleted successfully!"}
        else:
            return {"status": "error", "message": "Failed to delete expense."}
    except Exception as e:
        return {"status": "error", "message": f"Delete failed: {str(e)}"}


@mcp.tool()
async def update_expense(expense_id: str, phone: str, category: str, subcategory: str = "", note: str = ""):
    '''Update category/subcategory/note for an expense by id, verifying ownership.'''
    try:
        from bson import ObjectId
        
        if not expense_id or not phone:
            return {"status": "error", "message": "Expense ID and phone are required."}
        
        # Verify ownership before updating
        expense = await mongo_expenses.find_one({"_id": ObjectId(expense_id)})
        if not expense:
            return {"status": "error", "message": "Expense not found."}
        
        if expense.get("phone") != phone.strip():
            return {"status": "error", "message": "You can only update your own expenses."}
        
        result = await mongo_expenses.update_one(
            {"_id": ObjectId(expense_id)},
            {"$set": {"category": category, "subcategory": subcategory or "", "note": note or ""}}
        )
        if result.modified_count > 0:
            return {"status": "success", "updated": 1, "message": "Expense updated successfully!"}
        else:
            return {"status": "success", "updated": 0, "message": "No changes made."}
    except Exception as e:
        return {"status": "error", "message": f"Update failed: {str(e)}"}


@mcp.tool()
async def summarize(phone: str, start_date: str, end_date: str, category: str = None):
    '''Summarize expenses by category within an inclusive date range for a user.'''
    try:
        pipeline = [
            {"$match": {"phone": phone, "date": {"$gte": start_date, "$lte": end_date}}},
        ]
        
        if category:
            pipeline[0]["$match"]["category"] = category
        
        pipeline.extend([
            {"$group": {"_id": "$category", "total_amount": {"$sum": "$amount"}, "count": {"$sum": 1}}},
            {"$sort": {"total_amount": -1}}
        ])
        
        cursor = mongo_expenses.aggregate(pipeline)
        results = []
        async for doc in cursor:
            results.append({
                "category": doc["_id"],
                "total_amount": doc["total_amount"],
                "count": doc["count"]
            })
        return results
    except Exception as e:
        return {"status": "error", "message": f"Error summarizing expenses: {str(e)}"}


# ============== TRANSACTION PARSING ==============

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
            is_bank = True
            confidence = 0.9
            suggested_category = get_category_from_merchant(merchant)
            return {"status": "success", "amount": amount, "date": dt, "merchant": merchant, "note": t, "is_bank": is_bank, "confidence": confidence, "suggested_category": suggested_category}

        # Bank SMS: "debited for Rs.1.00 on 11-01-26 trf to SANDEEP GUPTA"
        m = re.search(r"debited for [₹Rs.]*([0-9,]+(?:\.[0-9]+)?) on ([0-9]{2}-[0-9]{2}-[0-9]{2,4})(?: .*to ([\w &.\-]+))?", t, re.IGNORECASE)
        if m:
            amount = float(m.group(1).replace(',', ''))
            raw_date = m.group(2)
            merchant = (m.group(3) or "").strip()
            parsed_date = None
            for fmt in ("%d-%m-%Y", "%d-%m-%y"):
                try:
                    parsed_date = datetime.strptime(raw_date, fmt).date().isoformat()
                    break
                except Exception:
                    continue
            if parsed_date is None:
                parsed_date = datetime.utcnow().date().isoformat()
            is_bank, confidence = detect_bank_sender(s)
            suggested_category = get_category_from_merchant(merchant) if merchant else "Bills & Utilities"
            return {"status": "success", "amount": amount, "date": parsed_date, "merchant": merchant or "Bank", "note": t, "is_bank": is_bank, "confidence": confidence, "suggested_category": suggested_category}

        # Email style: "Account ... has been DEBITED for Rs.1.00"
        m = re.search(r"DEBITED for [₹Rs.]*([0-9,]+(?:\.[0-9]+)?)", t, re.IGNORECASE)
        if m:
            amount = float(m.group(1).replace(',', ''))
            is_bank, confidence = detect_bank_sender(s)
            return {"status": "success", "amount": amount, "date": datetime.utcnow().date().isoformat(), "merchant": "Bank", "note": t, "is_bank": is_bank, "confidence": confidence, "suggested_category": "Bills & Utilities"}

        # Fallback: look for just an amount
        m = re.search(r"[₹Rs.]*([0-9,]+(?:\.[0-9]+)?)", t)
        if m:
            amount = float(m.group(1).replace(',', ''))
            is_bank, confidence = detect_bank_sender(s)
            return {"status": "success", "amount": amount, "date": datetime.utcnow().date().isoformat(), "merchant": "Unknown", "note": t, "is_bank": is_bank, "confidence": confidence, "suggested_category": "Other"}

        return {"status": "error", "message": "Could not parse transaction"}
    except Exception as e:
        return {"status": "error", "message": f"Parser error: {str(e)}"}


def detect_bank_sender(sender: str):
    """Detect if sender is a bank and return (is_bank, confidence)."""
    import re
    if not sender:
        return False, 0.3
    
    su = sender.upper()
    bank_keywords = ["KBL", "KARNATAKA", "SBI", "HDFC", "ICICI", "AXIS", "PNB", "YESBANK", "IDFC", "KOTAK", "CANARA", "BANK", "BNK", "PAYTM", "PHONEPE", "GOOGLEPAY", "GPAISA", "NBUPAISA"]
    
    if any(k in su for k in bank_keywords):
        return True, 0.95
    elif re.match(r"^[A-Z]{3,15}$", su):
        return True, 0.9
    elif re.match(r"^[0-9]{3,6}$", su):
        return True, 0.8
    return False, 0.3


def get_category_from_merchant(merchant: str):
    """Suggest category based on merchant name."""
    if not merchant:
        return "Other"
    
    mk = merchant.lower()
    if any(k in mk for k in ["uber", "ola", "taxi", "cab", "fuel", "petrol", "metro", "bus"]):
        return "Transportation"
    elif any(k in mk for k in ["restaurant", "cafe", "dine", "bar", "hotel", "food", "zomato", "swiggy"]):
        return "Food & Dining"
    elif any(k in mk for k in ["flipkart", "amazon", "myntra", "shop", "store", "super", "grocery", "grocer", "mall"]):
        return "Shopping"
    elif any(k in mk for k in ["netflix", "spotify", "movie", "cinema", "pvr", "inox"]):
        return "Entertainment"
    elif any(k in mk for k in ["hospital", "pharmacy", "doctor", "clinic", "medical", "health"]):
        return "Healthcare"
    elif any(k in mk for k in ["electricity", "water", "gas", "internet", "mobile", "recharge"]):
        return "Bills & Utilities"
    return "Other"


@mcp.tool()
async def categorize_transaction(text: str, sender: str = None):
    '''Return a suggested category for a free-form text using existing parser heuristics.'''
    try:
        parsed = await parse_transaction(text, sender)
        if isinstance(parsed, dict) and parsed.get("status") == "success":
            return {"status": "success", "suggested_category": parsed.get("suggested_category", "Other"), "confidence": parsed.get("confidence", 0.0)}
        return {"status": "error", "message": "Could not categorize"}
    except Exception as e:
        return {"status": "error", "message": f"Categorize error: {str(e)}"}


# ============== RESOURCES ==============

@mcp.resource("expense:///categories", mime_type="application/json")
def categories():
    try:
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


# ============== DEBUG TOOLS ==============

@mcp.tool()
async def debug_list_expenses():
    '''List all expenses from MongoDB, no filter.'''
    cursor = mongo_expenses.find({})
    expenses = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        expenses.append(doc)
    print("DEBUG: All expenses:", expenses)
    return expenses


@mcp.tool()
async def debug_list_expenses_by_date(date: str):
    '''List expenses from MongoDB for a specific date.'''
    cursor = mongo_expenses.find({"date": date})
    expenses = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        expenses.append(doc)
    print(f"DEBUG: Expenses for date {date}:", expenses)
    return expenses


# Start the server
if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8000)
