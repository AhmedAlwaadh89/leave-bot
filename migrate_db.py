import sqlite3

def migrate():
    conn = sqlite3.connect('leave_management.db')
    cursor = conn.cursor()

    # Check if 'department' column exists in 'employees' table
    cursor.execute("PRAGMA table_info(employees)")
    columns = [info[1] for info in cursor.fetchall()]
    if 'department' not in columns:
        print("Adding 'department' column to 'employees' table...")
        cursor.execute("ALTER TABLE employees ADD COLUMN department TEXT")
    else:
        print("'department' column already exists in 'employees' table.")

    # Check if 'replacement_approval_status' column exists in 'leave_requests' table
    cursor.execute("PRAGMA table_info(leave_requests)")
    columns = [info[1] for info in cursor.fetchall()]
    if 'replacement_approval_status' not in columns:
        print("Adding 'replacement_approval_status' column to 'leave_requests' table...")
        cursor.execute("ALTER TABLE leave_requests ADD COLUMN replacement_approval_status TEXT DEFAULT 'pending'")
    else:
        print("'replacement_approval_status' column already exists in 'leave_requests' table.")

    conn.commit()
    conn.close()

if __name__ == "__main__":
    migrate()
