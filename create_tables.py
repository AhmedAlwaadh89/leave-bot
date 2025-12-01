"""
Script to create database tables on Neon.tech
Run this script to ensure all tables are created properly.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# Import database module which will create tables
from database import engine, Base, run_migrations

def create_all_tables():
    """Create all tables and run migrations"""
    print("Creating database tables...")
    print(f"Database URL (masked): {os.getenv('DATABASE_URL', 'Not set')[:50]}...")
    
    try:
        # Create all tables
        Base.metadata.create_all(engine)
        print("✅ Tables created successfully!")
        
        # Run migrations to add missing columns
        print("\nRunning migrations...")
        run_migrations()
        print("✅ Migrations completed!")
        
        # List all tables
        from sqlalchemy import inspect
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        
        print("\n📋 Available tables:")
        for table in tables:
            columns = inspector.get_columns(table)
            print(f"\n  📁 {table}")
            print(f"     Columns: {len(columns)}")
            for col in columns:
                print(f"       - {col['name']} ({col['type']})")
        
        print("\n✅ All done! Your database is ready.")
        
        # Provide direct link to Neon.tech
        print("\n🔗 View your tables at:")
        print("   https://console.neon.tech/app/projects/tiny-dawn-20213424/branches/br-lingering-field-a4ehcdod/tables")
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    create_all_tables()
