"""Entry point — run with: python run.py"""
from app import app
import database as db

if __name__ == '__main__':
    db.init_db()
    print("\n✦ Soulful Content Engine")
    print("  Running at: http://127.0.0.1:5000\n")
    app.run(debug=True, port=5000)
