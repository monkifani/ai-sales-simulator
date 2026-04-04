import json
import os
import hashlib
import time
from datetime import datetime
from collections import defaultdict
import logging

class Database:
    """Database class with in-memory storage and JSON persistence."""
    
    DB_FILE = "database.json"
    
    def __init__(self):
        self.companies = {}
        self.users = {}
        self.sessions = []
        self.session_counter = 0
        self.load_from_file()
    
    def load_from_file(self):
        """Load data from JSON file if it exists."""
        if os.path.exists(self.DB_FILE):
            try:
                with open(self.DB_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.companies = data.get("companies", {})
                    self.users = data.get("users", {})
                    self.sessions = data.get("sessions", [])
                    self.session_counter = data.get("session_counter", 0)
                logging.info(f"Database loaded: {len(self.users)} users, {len(self.sessions)} sessions")
            except Exception as e:
                logging.error(f"Failed to load database: {e}")
    
    def save_to_file(self):
        """Save data to JSON file."""
        try:
            with open(self.DB_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "companies": self.companies,
                    "users": {str(k): v for k, v in self.users.items()},
                    "sessions": self.sessions,
                    "session_counter": self.session_counter,
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error(f"Failed to save database: {e}")

    def register_company(self, company_name: str, admin_id: int) -> str:
        """Register a new company."""
        raw = f"{company_name}{admin_id}{time.time()}"
        code = hashlib.md5(raw.encode()).hexdigest()[:8].upper()
        self.companies[code] = {
            "name": company_name,
            "admin_ids": [admin_id],
            "created_at": datetime.now().isoformat(),
            "plan": "free",
            "max_users": 10,
            "max_sessions_per_user": 50,
        }
        self.users[admin_id] = {
            "name": "",
            "company_code": code,
            "role": "admin",
            "registered_at": datetime.now().isoformat(),
        }
        self.save_to_file()
        return code

    def join_company(self, user_id: int, user_name: str, code: str) -> bool:
        """Join a company by code."""
        code = code.upper().strip()
        if code not in self.companies:
            return False
        current_users = sum(1 for u in self.users.values() if u.get("company_code") == code)
        if current_users >= self.companies[code]["max_users"]:
            return False
        self.users[user_id] = {
            "name": user_name,
            "company_code": code,
            "role": "manager",
            "registered_at": datetime.now().isoformat(),
        }
        self.save_to_file()
        return True

    def get_user(self, user_id: int):
        """Get user by ID."""
        return self.users.get(user_id)

    def get_company(self, code: str):
        """Get company by code."""
        return self.companies.get(code)

    def is_admin(self, user_id: int) -> bool:
        """Check if user is admin."""
        user = self.users.get(user_id)
        return user is not None and user.get("role") == "admin"

    def save_session(self, session_data: dict) -> int:
        """Save a session."""
        self.session_counter += 1
        session_data["session_id"] = self.session_counter
        session_data["completed_at"] = datetime.now().isoformat()
        self.sessions.append(session_data)
        self.save_to_file()
        return self.session_counter

    def get_user_sessions(self, user_id: int, limit: int = 10) -> list:
        """Get user sessions."""
        user_sessions = [s for s in self.sessions if s.get("user_id") == user_id]
        return sorted(user_sessions, key=lambda x: x.get("completed_at", ""), reverse=True)[:limit]

    def get_company_sessions(self, company_code: str, limit: int = 50) -> list:
        """Get company sessions."""
        company_sessions = [s for s in self.sessions if s.get("company_code") == company_code]
        return sorted(company_sessions, key=lambda x: x.get("completed_at", ""), reverse=True)[:limit]

    def get_company_leaderboard(self, company_code: str) -> list:
        """Get company leaderboard."""
        company_sessions = [s for s in self.sessions if s.get("company_code") == company_code]
        stats = defaultdict(lambda: {"scores": [], "sessions": 0, "name": ""})
        for s in company_sessions:
            uid = s.get("user_id")
            score = s.get("score", 0)
            stats[uid]["scores"].append(score)
            stats[uid]["sessions"] += 1
            stats[uid]["name"] = s.get("user_name", "Unknown")
        leaderboard = []
        for uid, data in stats.items():
            avg = sum(data["scores"]) / len(data["scores"]) if data["scores"] else 0
            best = max(data["scores"]) if data["scores"] else 0
            leaderboard.append({
                "user_id": uid,
                "name": data["name"],
                "avg_score": round(avg, 1),
                "best_score": best,
                "sessions": data["sessions"],
            })
        return sorted(leaderboard, key=lambda x: x["avg_score"], reverse=True)

    def get_user_stats(self, user_id: int) -> dict:
        """Get user statistics."""
        user_sessions = [s for s in self.sessions if s.get("user_id") == user_id]
        if not user_sessions:
            return {"sessions": 0, "avg_score": 0, "best_score": 0, "worst_score": 0, "avg_ai_detect": 0}
        scores = [s.get("score", 0) for s in user_sessions]
        ai_detects = [s.get("ai_detect_percent", 0) for s in user_sessions]
        return {
            "sessions": len(user_sessions),
            "avg_score": round(sum(scores) / len(scores), 1),
            "best_score": max(scores),
            "worst_score": min(scores),
            "avg_ai_detect": round(sum(ai_detects) / len(ai_detects), 1) if ai_detects else 0,
            "last_session": user_sessions[-1].get("completed_at", ""),
        }