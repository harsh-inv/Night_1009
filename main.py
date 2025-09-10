from fastapi import FastAPI, File, UploadFile, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import sqlite3
import csv
import io
import os
import tempfile
import shutil
from typing import Optional, List, Dict, Any
from datetime import datetime
import logging
from pydantic import BaseModel
import json

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Data Quality Checker API",
    description="API for running data quality checks on SQLite databases",
    version="1.0.0"
)

# Enable CORS for all origins (adjust for production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, replace with specific origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class DataQualityChecker:
    def __init__(self, db_connection):
        self.db_connection = db_connection
        self.checks_config = {}
        self.system_codes_config = {}

    def load_checks_config_from_content(self, csv_content: str) -> bool:
        """Load checks config from CSV content string"""
        try:
            reader = csv.DictReader(io.StringIO(csv_content))
            self.checks_config = {}
            for row in reader:
                table_name = row['table_name']
                field_name = row['field_name']
                if table_name not in self.checks_config:
                    self.checks_config[table_name] = {}
                self.checks_config[table_name][field_name] = {
                    'description': row.get('description', ''),
                    'special_characters_check': row.get('special_characters_check', '0') == '1',
                    'null_check': row.get('null_check', '0') == '1',
                    'blank_check': row.get('blank_check', '0') == '1',
                    'max_value_check': row.get('max_value_check', '0') == '1',
                    'min_value_check': row.get('min_value_check', '0') == '1',
                    'max_count_check': row.get('max_count_check', '0') == '1',
                    'email_check': row.get('email_check', '0') == '1',
                    'numeric_check': row.get('numeric_check', '0') == '1',
                    'system_codes_check': row.get('system_codes_check', '0') == '1',
                    'language_check': row.get('language_check', '0') == '1',
                    'phone_number_check': row.get('phone_number_check', '0') == '1',
                    'duplicate_check': row.get('duplicate_check', '0') == '1',
                    'date_check': row.get('date_check', '0') == '1'
                }
            return True
        except Exception as e:
            logger.error(f"Error loading checks configuration: {str(e)}")
            return False

    def load_system_codes_config_from_content(self, csv_content: str) -> bool:
        """Load system codes config from CSV content string"""
        try:
            self.system_codes_config = {}
            reader = csv.DictReader(io.StringIO(csv_content))
            for row in reader:
                table_name = row['table_name']
                field_name = row['field_name']
                valid_codes_str = row.get('valid_codes', '')
                valid_codes = [code.strip() for code in valid_codes_str.split(',') if code.strip()]
                if table_name not in self.system_codes_config:
                    self.system_codes_config[table_name] = {}
                self.system_codes_config[table_name][field_name] = valid_codes
            return True
        except Exception as e:
            logger.error(f"Error loading system codes configuration: {str(e)}")
            return False

    def _table_exists(self, table_name: str) -> bool:
        try:
            cursor = self.db_connection.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
            return cursor.fetchone() is not None
        except sqlite3.Error:
            return False

    def _column_exists(self, table_name: str, column_name: str) -> bool:
        try:
            cursor = self.db_connection.cursor()
            cursor.execute(f"PRAGMA table_info({table_name})")
            columns = [row[1] for row in cursor.fetchall()]
            return column_name in columns
        except sqlite3.Error:
            return False

    def _is_numeric(self, value: str) -> bool:
        try:
            float(value)
            return True
        except ValueError:
            return False

    def _is_valid_email(self, email: str) -> bool:
        import re
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        return re.match(email_pattern, email) is not None

    def _is_valid_phone(self, phone: str) -> bool:
        import re
        cleaned_phone = re.sub(r'[^\d+]', '', phone)
        if len(cleaned_phone) < 10 or len(cleaned_phone) > 15:
            return False
        phone_pattern = r'^\+?[1-9]\d{9,14}$'
        return re.match(phone_pattern, cleaned_phone) is not None

    def _is_valid_date(self, date_str: str) -> bool:
        date_formats = [
            '%Y-%m-%d', '%m/%d/%Y', '%d/%m/%Y', '%Y-%m-%d %H:%M:%S',
            '%m-%d-%Y', '%d-%m-%Y', '%Y/%m/%d', '%d.%m.%Y',
            '%Y', '%m/%Y', '%Y-%m'
        ]
        for fmt in date_formats:
            try:
                datetime.strptime(str(date_str), fmt)
                return True
            except ValueError:
                continue
        return False

    def _has_special_characters(self, text: str) -> bool:
        import re
        allowed_pattern = r'^[a-zA-Z0-9\s.,@_-]+$'
        return not re.match(allowed_pattern, text)

    def _has_non_ascii_characters(self, text: str) -> bool:
        try:
            text.encode('ascii')
            return False
        except UnicodeEncodeError:
            return True

    def _get_valid_system_codes(self, table_name: str, field_name: str) -> List[str]:
        return self.system_codes_config.get(table_name, {}).get(field_name, [])

    def _looks_like_system_code(self, code: str) -> bool:
        import re
        patterns = [
            r'^[A-F0-9]{8}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{12}$',
            r'^[A-Z]{2,3}\d{3,}$',
            r'^\d{6,}$',
            r'^[A-Z0-9]{8,}$',
        ]
        for pattern in patterns:
            if re.match(pattern, code.upper()):
                return True
        return False

    def _run_field_checks(self, table_name: str, field_name: str, checks: Dict) -> List[Dict]:
        results = []
        if not self._column_exists(table_name, field_name):
            results.append({
                'table': table_name,
                'field': field_name,
                'check_type': 'column_existence',
                'status': 'FAIL',
                'message': f"Column '{field_name}' does not exist in table '{table_name}'"
            })
            return results

        try:
            cursor = self.db_connection.cursor()
            cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
            total_rows = cursor.fetchone()[0]

            if total_rows == 0:
                results.append({
                    'table': table_name,
                    'field': field_name,
                    'check_type': 'data_existence',
                    'status': 'WARNING',
                    'message': f"Table '{table_name}' has no data"
                })
                return results

            # Null check
            if checks.get('null_check', False):
                cursor.execute(f"SELECT COUNT(*) FROM {table_name} WHERE {field_name} IS NULL")
                null_count = cursor.fetchone()[0]
                if null_count > 0:
                    results.append({
                        'table': table_name,
                        'field': field_name,
                        'check_type': 'null_check',
                        'status': 'FAIL',
                        'message': f"Found {null_count} NULL values out of {total_rows} total rows"
                    })
                else:
                    results.append({
                        'table': table_name,
                        'field': field_name,
                        'check_type': 'null_check',
                        'status': 'PASS',
                        'message': f"No NULL values found"
                    })

            # Blank check
            if checks.get('blank_check', False):
                cursor.execute(f"SELECT COUNT(*) FROM {table_name} WHERE {field_name} = ''")
                blank_count = cursor.fetchone()[0]
                if blank_count > 0:
                    results.append({
                        'table': table_name,
                        'field': field_name,
                        'check_type': 'blank_check',
                        'status': 'FAIL',
                        'message': f"Found {blank_count} blank values out of {total_rows} total rows"
                    })
                else:
                    results.append({
                        'table': table_name,
                        'field': field_name,
                        'check_type': 'blank_check',
                        'status': 'PASS',
                        'message': f"No blank values found"
                    })

            # Email check
            if checks.get('email_check', False):
                cursor.execute(f"SELECT COUNT(*) FROM {table_name} WHERE {field_name} IS NOT NULL AND {field_name} != ''")
                non_null_count = cursor.fetchone()[0]
                if non_null_count > 0:
                    cursor.execute(f"SELECT {field_name} FROM {table_name} WHERE {field_name} IS NOT NULL AND {field_name} != ''")
                    values = cursor.fetchall()
                    invalid_emails = []
                    for value in values:
                        email = str(value[0]).strip()
                        if not self._is_valid_email(email):
                            invalid_emails.append(email)

                    if invalid_emails:
                        results.append({
                            'table': table_name,
                            'field': field_name,
                            'check_type': 'email_check',
                            'status': 'FAIL',
                            'message': f"Found {len(invalid_emails)} invalid email formats out of {non_null_count} values"
                        })
                    else:
                        results.append({
                            'table': table_name,
                            'field': field_name,
                            'check_type': 'email_check',
                            'status': 'PASS',
                            'message': f"All {non_null_count} email formats appear valid"
                        })

            # System codes check
            if checks.get('system_codes_check', False):
                cursor.execute(f"SELECT COUNT(*) FROM {table_name} WHERE {field_name} IS NOT NULL AND {field_name} != ''")
                non_null_count = cursor.fetchone()[0]
                if non_null_count > 0:
                    cursor.execute(f"SELECT DISTINCT {field_name} FROM {table_name} WHERE {field_name} IS NOT NULL AND {field_name} != ''")
                    values = cursor.fetchall()
                    valid_codes_list = self._get_valid_system_codes(table_name, field_name)
                    invalid_system_codes = []
                    for value in values:
                        code = str(value[0]).strip().upper()
                        valid_codes_upper = [vc.upper() for vc in valid_codes_list] if valid_codes_list else []
                        if valid_codes_list and code not in valid_codes_upper:
                            invalid_system_codes.append(str(value[0]).strip())
                        elif not valid_codes_list and not self._looks_like_system_code(code):
                            invalid_system_codes.append(str(value[0]).strip())

                    if invalid_system_codes:
                        if valid_codes_list:
                            message = f"Found {len(invalid_system_codes)} invalid system codes out of {non_null_count} values"
                            message += f" (Valid codes: {len(valid_codes_list)} defined)"
                        else:
                            message = f"Found {len(invalid_system_codes)} values that don't match system code patterns out of {non_null_count} values"
                        results.append({
                            'table': table_name,
                            'field': field_name,
                            'check_type': 'system_codes_check',
                            'status': 'FAIL',
                            'message': message
                        })
                    else:
                        if valid_codes_list:
                            results.append({
                                'table': table_name,
                                'field': field_name,
                                'check_type': 'system_codes_check',
                                'status': 'PASS',
                                'message': f"All {non_null_count} values are valid system codes from external config ({len(valid_codes_list)} codes)"
                            })
                        else:
                            results.append({
                                'table': table_name,
                                'field': field_name,
                                'check_type': 'system_codes_check',
                                'status': 'PASS',
                                'message': f"All {non_null_count} values match system code patterns"
                            })

        except sqlite3.Error as e:
            results.append({
                'table': table_name,
                'field': field_name,
                'check_type': 'database_error',
                'status': 'ERROR',
                'message': f"Database error: {str(e)}"
            })

        return results

    def run_all_checks(self) -> Dict[str, List[Dict]]:
        if not self.checks_config:
            return {}

        results = {}
        for table_name, fields in self.checks_config.items():
            if not self._table_exists(table_name):
                continue

            table_results = []
            for field_name, checks in fields.items():
                field_results = self._run_field_checks(table_name, field_name, checks)
                if field_results:
                    table_results.extend(field_results)

            if table_results:
                results[table_name] = table_results

        return results

# Global variables - Initialize with demo database on startup
db_connection = None
data_quality_checker = None

def initialize_database():
    """Initialize database connection on startup"""
    global db_connection, data_quality_checker
    
    try:
        # Create a demo database with sample data
        temp_db_path = tempfile.mktemp(suffix='.db')
        db_connection = sqlite3.connect(temp_db_path, check_same_thread=False)
        db_connection.row_factory = sqlite3.Row
        
        # Create sample tables and data for testing
        cursor = db_connection.cursor()
        
        # Users table
        cursor.execute("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                name TEXT,
                email TEXT,
                phone TEXT,
                status_code TEXT
            )
        """)
        
        cursor.execute("""
            INSERT INTO users (name, email, phone, status_code) VALUES 
            ('John Doe', 'john@example.com', '+1234567890', 'ACT001'),
            ('Jane Smith', 'jane@invalid-email', '123', 'INA002'),
            ('Bob Wilson', 'bob@test.com', '+9876543210', 'ACT001'),
            ('Alice Johnson', '', '+1122334455', 'PEN003'),
            ('Charlie Brown', 'charlie@demo.com', 'invalid-phone', 'ACT001')
        """)
        
        # Orders table
        cursor.execute("""
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                customer_name TEXT,
                order_date TEXT,
                amount REAL,
                order_status TEXT
            )
        """)
        
        cursor.execute("""
            INSERT INTO orders (customer_name, order_date, amount, order_status) VALUES 
            ('John Doe', '2024-01-15', 299.99, 'COMPLETED'),
            ('Jane Smith', 'invalid-date', 199.50, 'PENDING'),
            ('Bob Wilson', '2024-01-16', -50.00, 'CANCELLED'),
            ('Alice Johnson', '', 399.99, 'SHIPPED'),
            ('Charlie Brown', '2024-01-17', 0, 'PROCESSING')
        """)
        
        db_connection.commit()
        
        # Initialize data quality checker
        data_quality_checker = DataQualityChecker(db_connection)
        
        logger.info(f"Database initialized successfully at {temp_db_path}")
        
    except Exception as e:
        logger.error(f"Error initializing database: {str(e)}")

# Initialize database on startup
initialize_database()

# Pydantic models for API requests/responses
class CheckResult(BaseModel):
    table: str
    field: str
    check_type: str
    status: str
    message: str

class CheckResults(BaseModel):
    results: Dict[str, List[CheckResult]]
    summary: Dict[str, int]
    timestamp: str

@app.get("/")
async def root():
    """API status endpoint"""
    global db_connection, data_quality_checker
    
    # Get database info
    tables = []
    if db_connection:
        cursor = db_connection.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row[0] for row in cursor.fetchall()]
    
    return {
        "message": "Data Quality Checker API - Ready to Use",
        "version": "1.0.0",
        "database_status": "Connected" if db_connection else "Not Connected",
        "available_tables": tables,
        "workflow": [
            "1. Upload data quality configuration CSV",
            "2. Upload system codes configuration CSV (optional)",
            "3. Run data quality checks",
            "4. View results"
        ]
    }

@app.post("/upload-data-quality-config")
async def upload_data_quality_config(file: UploadFile = File(...)):
    """Upload data quality configuration CSV file"""
    global data_quality_checker

    if not data_quality_checker:
        raise HTTPException(status_code=500, detail="Database not initialized")

    if not file.filename.endswith('.csv'):
        raise HTTPException(status_code=400, detail="File must be a CSV file")

    try:
        content = await file.read()
        csv_content = content.decode('utf-8')
        success = data_quality_checker.load_checks_config_from_content(csv_content)

        if success:
            configured_tables = list(data_quality_checker.checks_config.keys())
            return {
                "message": "Data quality configuration loaded successfully",
                "status": "success",
                "configured_tables": configured_tables,
                "total_tables": len(configured_tables),
                "next_step": "Upload system codes configuration (optional) or run checks"
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to load data quality configuration")

    except Exception as e:
        logger.error(f"Error uploading data quality config: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error processing file: {str(e)}")

@app.post("/upload-system-codes-config")
async def upload_system_codes_config(file: UploadFile = File(...)):
    """Upload system codes configuration CSV file"""
    global data_quality_checker

    if not data_quality_checker:
        raise HTTPException(status_code=500, detail="Database not initialized")

    if not file.filename.endswith('.csv'):
        raise HTTPException(status_code=400, detail="File must be a CSV file")

    try:
        content = await file.read()
        csv_content = content.decode('utf-8')
        success = data_quality_checker.load_system_codes_config_from_content(csv_content)

        if success:
            configured_tables = list(data_quality_checker.system_codes_config.keys())
            return {
                "message": "System codes configuration loaded successfully",
                "status": "success",
                "configured_tables": configured_tables,
                "total_tables": len(configured_tables),
                "next_step": "Ready to run data quality checks"
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to load system codes configuration")

    except Exception as e:
        logger.error(f"Error uploading system codes config: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error processing file: {str(e)}")

@app.post("/run-checks")
async def run_checks():
    """Run all configured data quality checks"""
    global data_quality_checker

    if not data_quality_checker:
        raise HTTPException(status_code=500, detail="Database not initialized")

    if not data_quality_checker.checks_config:
        raise HTTPException(status_code=400, detail="No data quality checks configured. Please upload configuration first.")

    try:
        results = data_quality_checker.run_all_checks()

        # Calculate summary statistics
        total_checks = 0
        passed_checks = 0
        failed_checks = 0
        warnings = 0
        errors = 0

        # Flatten results for summary
        flat_results = []
        for table_name, table_results in results.items():
            for result in table_results:
                flat_results.append(result)
                total_checks += 1
                status = result['status']
                if status == 'PASS':
                    passed_checks += 1
                elif status == 'FAIL':
                    failed_checks += 1
                elif status == 'WARNING':
                    warnings += 1
                elif status == 'ERROR':
                    errors += 1

        summary = {
            "total_checks": total_checks,
            "passed_checks": passed_checks,
            "failed_checks": failed_checks,
            "warnings": warnings,
            "errors": errors,
            "success_rate": round((passed_checks / total_checks * 100), 2) if total_checks > 0 else 0
        }

        return {
            "message": "Data quality checks completed successfully",
            "status": "completed",
            "results": results,
            "summary": summary,
            "timestamp": datetime.now().isoformat(),
            "total_tables_checked": len(results)
        }

    except Exception as e:
        logger.error(f"Error running data quality checks: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error running checks: {str(e)}")

@app.get("/status")
async def get_status():
    """Get current API and configuration status"""
    global db_connection, data_quality_checker
    
    # Get database tables
    tables = []
    if db_connection:
        cursor = db_connection.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row[0] for row in cursor.fetchall()]

    return {
        "api_status": "Ready",
        "database_connected": db_connection is not None,
        "data_quality_checker_initialized": data_quality_checker is not None,
        "checks_configured": bool(data_quality_checker and data_quality_checker.checks_config),
        "system_codes_configured": bool(data_quality_checker and data_quality_checker.system_codes_config),
        "configured_tables": list(data_quality_checker.checks_config.keys()) if data_quality_checker else [],
        "available_database_tables": tables,
        "timestamp": datetime.now().isoformat(),
        "ready_to_run": bool(data_quality_checker and data_quality_checker.checks_config)
    }

@app.get("/database-info")
async def get_database_info():
    """Get database schema information"""
    global db_connection

    if not db_connection:
        raise HTTPException(status_code=500, detail="Database not connected")

    try:
        cursor = db_connection.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()

        schema_info = []
        for table in tables:
            table_name = table[0]
            cursor.execute(f"PRAGMA table_info({table_name});")
            columns = cursor.fetchall()
            
            # Get row count
            cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
            row_count = cursor.fetchone()[0]

            column_info = []
            for col in columns:
                column_info.append({
                    "name": col[1],
                    "type": col[2],
                    "nullable": not bool(col[3]),
                    "default_value": col[4],
                    "primary_key": bool(col[5])
                })

            schema_info.append({
                "table_name": table_name,
                "row_count": row_count,
                "columns": column_info,
                "total_columns": len(column_info)
            })

        return {
            "database_schema": schema_info,
            "total_tables": len(schema_info),
            "timestamp": datetime.now().isoformat()
        }

    except Exception as e:
        logger.error(f"Error getting database schema: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error getting schema: {str(e)}")

@app.delete("/reset")
async def reset_configuration():
    """Reset configurations but keep database connection"""
    global data_quality_checker

    try:
        if data_quality_checker:
            data_quality_checker.checks_config = {}
            data_quality_checker.system_codes_config = {}

        return {
            "message": "Configuration reset successfully",
            "status": "reset",
            "timestamp": datetime.now().isoformat(),
            "next_step": "Upload new configuration files"
        }

    except Exception as e:
        logger.error(f"Error resetting configuration: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error resetting: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
