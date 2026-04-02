#!/usr/bin/env python3
"""
CLI to create user accounts for Propilot.
Usage: python3 add_user.py
"""
import json
import os
import getpass
from werkzeug.security import generate_password_hash

USERS_FILE = os.path.join(os.path.dirname(__file__), "users.json")


def load_users():
    if not os.path.exists(USERS_FILE):
        return []
    with open(USERS_FILE) as f:
        return json.load(f)


def save_users(users):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)


def main():
    users = load_users()

    print("\n── Propilot: Add User ──────────────────")
    username = input("Username: ").strip().lower()

    if any(u["username"] == username for u in users):
        print(f"Error: user '{username}' already exists.")
        return

    password = getpass.getpass("Password: ")
    confirm  = getpass.getpass("Confirm password: ")

    if password != confirm:
        print("Error: passwords don't match.")
        return

    if len(password) < 8:
        print("Error: password must be at least 8 characters.")
        return

    new_id = max((u["id"] for u in users), default=0) + 1
    users.append({
        "id":            new_id,
        "username":      username,
        "password_hash": generate_password_hash(password),
    })
    save_users(users)
    print(f"\n✓ User '{username}' created (id={new_id})")


if __name__ == "__main__":
    main()
