"""
verify_password.py
-------------------
The simplest possible password check -- no hashing, no sessions, just
string equality against a configured value. This is what gates the
analyst/SQL mode (see guardrails.sql_password_guardrail).

Run this file directly to test the exact same check you'd get from
plain `input()`-based Python at a terminal:

    python verify_password.py

In the Gradio app, the password comes from a masked Textbox instead of
`input()` (there's no terminal in a web UI), but it's checked by this
same verify_password() function either way.
"""
import os

ANALYST_PASSWORD = os.getenv("ANALYST_PASSWORD", "abcdef")


def verify_password(entered: str) -> bool:
    return entered == ANALYST_PASSWORD


if __name__ == "__main__":
    entered = input("Enter analyst password: ")
    if verify_password(entered):
        print("✅ Access granted.")
    else:
        print("❌ Access denied.")
