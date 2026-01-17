import streamlit as st
import pandas as pd
import numpy as np
from typing import Dict, List, Any
import re
from pathlib import Path
from dataclasses import dataclass
import os
import math
from datetime import datetime, date
import json

# Load Common CSS
def load_css(file_name="style.css"):
    """Loads CSS file. Automatically handles cases where script is inside a 'pages' subdirectory."""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.basename(current_dir) == "pages":
        css_path = os.path.join(current_dir, "..", file_name)
    else:
        css_path = os.path.join(current_dir, file_name)
    css_path = os.path.normpath(css_path)
    if os.path.exists(css_path):
        with open(css_path, 'r') as f:
            st.markdown(f'<style>{f.read()}</style>', unsafe_allow_html=True)

# --- Data Classes ---
@dataclass
class CalculationResult:
    formula_name: str
    rows_calculated: int
    errors: List[str]
    success_rate: float

@dataclass
class VariableMapping:
    variable_name: str
    mapped_header: str
    confidence_score: float = 0.0
    matching_method: str = "manual"
    is_verified: bool = True

# --- Mapping Import/Export Functions ---
def export_mappings_to_json(mappings: Dict[str, str]) -> str:
    """Export mappings to JSON string"""
    return json.dumps(mappings, indent=2)

def export_mappings_to_excel(mappings: Dict[str, str]) -> bytes:
    """Export mappings to Excel bytes"""
    from io import BytesIO
    
    # Create DataFrame from mappings
    df = pd.DataFrame([
        {"Excel_Header": header, "Variable_Name": var_name}
        for header, var_name in mappings.items()
    ])
    
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Mappings')
    
    return output.getvalue()

def import_mappings_from_json(json_file) -> Dict[str, str]:
    """Import mappings from JSON file - expects Excel_Header -> Variable_Name format"""
    try:
        content = json_file.read()
        mappings = json.loads(content)
        
        # Validate structure
        if not isinstance(mappings, dict):
            raise ValueError("JSON must contain a dictionary/object mapping Excel headers to variable names")
        
        # Convert all keys and values to strings and validate
        clean_mappings = {}
        for k, v in mappings.items():
            header = str(k).strip()
            var_name = str(v).strip()
            
            if header and var_name and header != 'nan' and var_name != 'nan':
                clean_mappings[header] = var_name
        
        if not clean_mappings:
            raise ValueError("No valid mappings found in JSON")
        
        return clean_mappings
    
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON format: {str(e)}")
    except Exception as e:
        raise ValueError(f"Error reading JSON: {str(e)}")

def import_mappings_from_excel(excel_file) -> Dict[str, str]:
    """Import mappings from Excel file"""
    try:
        df = pd.read_excel(excel_file)
        
        # Check for required columns
        if 'Excel_Header' not in df.columns or 'Variable_Name' not in df.columns:
            raise ValueError("Excel file must contain 'Excel_Header' and 'Variable_Name' columns")
        
        # Create mappings dictionary
        mappings = {}
        for _, row in df.iterrows():
            header = str(row['Excel_Header']).strip()
            var_name = str(row['Variable_Name']).strip()
            
            if header and var_name and header != 'nan' and var_name != 'nan':
                mappings[header] = var_name
        
        return mappings
    
    except Exception as e:
        raise ValueError(f"Error reading Excel: {str(e)}")

# --- Formula Import Functions ---
def import_formulas_from_json(json_file) -> List[Dict]:
    """Import formulas from JSON file - supports two formats:
    1. Direct list of formula objects
    2. Wrapped in extraction_summary object with 'formulas' key
    """
    try:
        content = json_file.read()
        data = json.loads(content)
        
        # Check if it's the wrapped format with extraction_summary
        if isinstance(data, dict) and 'formulas' in data:
            formulas = data['formulas']
            st.info(f"📊 Detected extraction format. Confidence: {data.get('overall_confidence', 'N/A')}")
        elif isinstance(data, list):
            formulas = data
        else:
            raise ValueError("JSON must contain either a list of formulas or an object with 'formulas' key")
        
        # Validate each formula has required fields
        validated_formulas = []
        for i, formula in enumerate(formulas):
            if not isinstance(formula, dict):
                raise ValueError(f"Formula {i} must be a dictionary/object")
            
            if 'formula_name' not in formula or 'formula_expression' not in formula:
                raise ValueError(f"Formula {i} must contain 'formula_name' and 'formula_expression' fields")
            
            # Clean up formula expression - remove variable assignment if present
            expr = formula['formula_expression']
            
            # Remove patterns like "VARIABLE_NAME = " from the beginning
            if '=' in expr:
                parts = expr.split('=', 1)
                if len(parts) == 2:
                    # Check if left side is just a variable name (matches the formula name)
                    left_side = parts[0].strip()
                    if left_side == formula['formula_name'] or left_side.replace('_', '').isalnum():
                        expr = parts[1].strip()
            
            # Remove square brackets if present
            expr = expr.strip('[]')
            
            # Replace MAX/MIN with lowercase for safe_eval
            expr = expr.replace('MAX(', 'max(').replace('MIN(', 'min(')
            
            validated_formulas.append({
                'formula_name': formula['formula_name'],
                'formula_expression': expr,
                'description': formula.get('description', ''),
                'variables_used': formula.get('variables_used', '')
            })
        
        return validated_formulas
    
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON format: {str(e)}")
    except Exception as e:
        raise ValueError(f"Error reading JSON: {str(e)}")

def import_formulas_from_excel(excel_file) -> List[Dict]:
    """Import formulas from Excel file"""
    try:
        df = pd.read_excel(excel_file)
        
        # Check for required columns
        required_cols = ['formula_name', 'formula_expression']
        missing_cols = [col for col in required_cols if col not in df.columns]
        
        if missing_cols:
            raise ValueError(f"Excel file must contain columns: {', '.join(missing_cols)}")
        
        # Create formulas list
        formulas = []
        for _, row in df.iterrows():
            formula_name = str(row['formula_name']).strip()
            formula_expr = str(row['formula_expression']).strip()
            
            if formula_name and formula_expr and formula_name != 'nan' and formula_expr != 'nan':
                formula_dict = {
                    'formula_name': formula_name,
                    'formula_expression': formula_expr
                }
                
                # Add optional fields if present
                if 'description' in df.columns and pd.notna(row.get('description')):
                    formula_dict['description'] = str(row['description']).strip()
                
                if 'variables_used' in df.columns and pd.notna(row.get('variables_used')):
                    formula_dict['variables_used'] = str(row['variables_used']).strip()
                
                formulas.append(formula_dict)
        
        return formulas
    
    except Exception as e:
        raise ValueError(f"Error reading Excel: {str(e)}")

def export_formulas_to_json(formulas: List[Dict]) -> str:
    """Export formulas to JSON string"""
    return json.dumps(formulas, indent=2)

def export_formulas_to_excel(formulas: List[Dict]) -> bytes:
    """Export formulas to Excel bytes"""
    from io import BytesIO
    
    # Create DataFrame from formulas
    df = pd.DataFrame(formulas)
    
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Formulas')
    
    return output.getvalue()

# --- Helper Functions ---
def safe_convert_to_number(value: Any) -> float:
    """Safely convert various types to float, handling dates, timestamps, etc."""
    
    # Handle None/NaN - return 0 for calculations
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0.0
    
    # Handle empty strings
    if isinstance(value, str) and (value == '' or value.strip() == ''):
        return 0.0
    
    # Already a number - return as is
    if isinstance(value, (int, float)) and not pd.isna(value):
        return float(value)
    
    # Handle datetime/timestamp - convert to year
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return float(value.year)
    
    # Handle strings
    if isinstance(value, str):
        # Try to parse as number
        try:
            # Remove common formatting characters
            cleaned = value.replace(',', '').replace('$', '').replace('%', '').strip()
            if cleaned:
                return float(cleaned)
            return 0.0
        except ValueError:
            # If it's a date string, try to parse it
            try:
                parsed_date = pd.to_datetime(value)
                return float(parsed_date.year)
            except:
                # Can't convert, return 0
                return 0.0
    
    # Default fallback
    return 0.0


def safe_eval(expression: str, variables: Dict[str, Any]) -> Any:
    """Safely evaluate a mathematical expression with given variables"""
    try:
        # Replace variable names with their values
        eval_expr = expression
        
        # Sort by length descending to avoid partial replacements
        sorted_vars = sorted(variables.keys(), key=len, reverse=True)
        
        for var_name in sorted_vars:
            # Use word boundaries to ensure exact matches
            pattern = r'\b' + re.escape(var_name) + r'\b'
            value = variables[var_name]
            
            # Convert to safe numeric value
            numeric_value = safe_convert_to_number(value)
            
            eval_expr = re.sub(pattern, str(numeric_value), eval_expr)
        
        # Handle common functions
        eval_expr = eval_expr.replace('MAX(', 'max(')
        eval_expr = eval_expr.replace('MIN(', 'min(')
        eval_expr = eval_expr.replace('ABS(', 'abs(')
        eval_expr = eval_expr.replace('POWER(', 'pow(')
        eval_expr = eval_expr.replace('SQRT(', 'math.sqrt(')
        
        # Safe evaluation with limited builtins
        allowed_builtins = {
            'max': max, 
            'min': min, 
            'abs': abs, 
            'round': round,
            'int': int,
            'float': float,
            'pow': pow,
            'sqrt': math.sqrt
        }
        
        result = eval(eval_expr, {"__builtins__": allowed_builtins, "math": math}, {})
        
        # Ensure result is a number
        if isinstance(result, (int, float)) and not (isinstance(result, float) and (math.isnan(result) or math.isinf(result))):
            return float(result)
        else:
            return None
    
    except Exception as e:
        # Return None instead of error string
        print(f"Evaluation error for '{expression}': {str(e)}")
        return None


def calculate_row(row: pd.Series, formula_expr: str, header_to_var_mapping: Dict[str, str]) -> Any:
    """Calculate formula result for a single row
    
    Args:
        row: DataFrame row
        formula_expr: Formula expression string
        header_to_var_mapping: Dict mapping Excel headers to variable names
    """
    
    # Build variable values from row data
    # We need to REVERSE the mapping: var_name -> header -> value
    var_values = {}
    
    # Reverse the mapping to go from variable name to header
    var_to_header = {v: k for k, v in header_to_var_mapping.items()}
    
    for var_name, header in var_to_header.items():
        if header in row.index:
            value = row[header]
            var_values[var_name] = value
    
    # Evaluate formula with the mapped values
    result = safe_eval(formula_expr, var_values)
    return result


def match_formula_to_output_column(formula_name: str, output_columns: List[str]) -> str:
    """
    Try to intelligently match a formula name to an output column
    Returns the best matching column name or the formula name if no match
    """
    formula_lower = formula_name.lower()
    
    # Direct exact match (case insensitive)
    for col in output_columns:
        if col.lower() == formula_lower:
            return col
    
    # Partial match - formula name in column or vice versa
    for col in output_columns:
        col_lower = col.lower()
        if formula_lower in col_lower or col_lower in formula_lower:
            return col
    
    # Token-based matching
    formula_tokens = set(re.findall(r'\w+', formula_lower))
    best_match = None
    best_score = 0
    
    for col in output_columns:
        col_tokens = set(re.findall(r'\w+', col.lower()))
        overlap = len(formula_tokens & col_tokens)
        
        if overlap > best_score:
            best_score = overlap
            best_match = col
    
    if best_match and best_score > 0:
        return best_match
    
    # No good match found, use formula name as new column
    return formula_name


def run_calculations(df: pd.DataFrame, 
                     formulas: List[Dict], 
                     header_to_var_mapping: Dict[str, str],
                     output_columns: List[str]) -> tuple[pd.DataFrame, List[CalculationResult]]:
    """Run selected formulas on dataframe
    
    Args:
        df: Input DataFrame
        formulas: List of formula dictionaries
        header_to_var_mapping: Dict mapping Excel headers to variable names
        output_columns: List of output column names to fill
    """
    result_df = df.copy()
    calculation_results = []
    
    # If no output columns specified, try to match all formulas
    if not output_columns:
        st.warning("⚠️ No output columns selected. Creating new columns for all formulas.")
        formulas_to_run = [(f, f.get('formula_name', 'Unknown')) for f in formulas]
    else:
        # Filter formulas - only run those that match output columns
        formulas_to_run = []
        for formula in formulas:
            formula_name = formula.get('formula_name', 'Unknown')
            
            # Check if this formula is relevant to any output column
            matched_col = match_formula_to_output_column(formula_name, output_columns)
            
            if matched_col in output_columns or matched_col == formula_name:
                formulas_to_run.append((formula, matched_col))
        
        if not formulas_to_run:
            st.warning("⚠️ No formulas matched the selected output columns. Using all formulas.")
            formulas_to_run = [(f, f.get('formula_name', 'Unknown')) for f in formulas]
    
    st.info(f"Running {len(formulas_to_run)} formula(s)")
    
    for formula, output_col in formulas_to_run:
        formula_name = formula.get('formula_name', 'Unknown')
        formula_expr = formula.get('formula_expression', '')
        
        st.info(f"🔧 Processing formula: **{formula_name}** → **{output_col}**")
        st.code(f"Expression: {formula_expr}")
        
        errors = []
        success_count = 0
        
        # Ensure column exists in dataframe - create if needed
        if output_col not in result_df.columns:
            result_df[output_col] = np.nan
            st.info(f"Created new column: {output_col}")
        
        # Create a progress bar
        progress_text = f"Processing: {formula_name} → {output_col}"
        progress_bar = st.progress(0)
        status_text = st.empty()
        status_text.info(progress_text)
        
        total_rows = len(result_df)
        
        # Debug: show first row calculation
        if total_rows > 0:
            first_row = result_df.iloc[0]
            
            # Build debug info
            var_to_header = {v: k for k, v in header_to_var_mapping.items()}
            var_values_debug = {}
            for var_name, header in var_to_header.items():
                if header in first_row.index:
                    var_values_debug[var_name] = first_row[header]
            
            st.write(f"📊 **Sample calculation (Row 0):**")
            st.write(f"Variables available: {list(var_values_debug.keys())}")
            st.write(f"Sample values: {dict(list(var_values_debug.items())[:5])}")
            
            first_result = calculate_row(first_row, formula_expr, header_to_var_mapping)
            st.write(f"**Calculated Result:** {first_result}")
        
        # Calculate for each row
        for idx in range(len(result_df)):
            try:
                row = result_df.iloc[idx]
                result = calculate_row(row, formula_expr, header_to_var_mapping)
                
                # Check if result is valid
                if result is None:
                    errors.append(f"Row {idx}: Calculation returned None")
                    result_df.at[result_df.index[idx], output_col] = np.nan
                else:
                    # CRITICAL FIX: Use .at with the actual index, not integer position
                    result_df.at[result_df.index[idx], output_col] = result
                    success_count += 1
            
            except Exception as e:
                error_msg = f"Row {idx}: {str(e)}"
                errors.append(error_msg)
                result_df.at[result_df.index[idx], output_col] = np.nan
            
            # Update Progress every 10 rows or at the end
            if idx % 10 == 0 or idx == total_rows - 1:
                progress = min((idx + 1) / total_rows, 1.0)
                progress_bar.progress(progress)
                status_text.text(f"{progress_text} ({idx+1}/{total_rows})")
        
        progress_bar.empty()
        status_text.empty()
        
        success_rate = (success_count / total_rows) * 100 if total_rows > 0 else 0
        
        # Show results count
        non_null_count = result_df[output_col].notna().sum()
        st.success(f"✅ Completed: {success_count}/{total_rows} rows ({success_rate:.1f}% success)")
        st.info(f"Non-null values in {output_col}: {non_null_count}")
        
        # Show sample of calculated values
        if non_null_count > 0:
            sample_vals = result_df[output_col].dropna().head(5).tolist()
            st.write(f"Sample calculated values: {sample_vals}")
        
        calculation_results.append(CalculationResult(
            formula_name=f"{formula_name} → {output_col}",
            rows_calculated=success_count,
            errors=errors[:20],  # Limit to first 20 errors
            success_rate=success_rate
        ))
    
    return result_df, calculation_results


# --- Main App ---
def main():
    st.set_page_config(
        page_title="Calculation Engine",
        page_icon="🧮",
        layout="wide"
    )
    
    load_css()
    
    st.markdown(
        """
        <div class="header-container">
            <div class="header-bar">
                <img src="https://raw.githubusercontent.com/AyushiR0y/streamlit_formulagen/main/assets/logo.png" style="height: 100px;">
                <div class="header-title" style="font-size: 2.5rem; font-weight: 750; color: #004DA8;">
                    Calculation Engine
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )
    
    st.markdown("---")
    
    # Check if mappings and formulas exist
    has_mappings = 'header_to_var_mapping' in st.session_state and st.session_state.header_to_var_mapping
    has_formulas = 'formulas' in st.session_state and st.session_state.formulas
    
    # === UPLOAD SECTION (only show if missing) ===
    if not has_mappings or not has_formulas:
        st.markdown("---")
        st.subheader("📤 Upload Configuration Files")
        
        col_upload1, col_upload2 = st.columns(2)
        
        # === MAPPINGS UPLOAD ===
        with col_upload1:
            st.markdown("### 📋 Variable Mappings")
            if has_mappings:
                st.success(f"✅ {len(st.session_state.header_to_var_mapping)} mappings loaded")
            
            uploaded_mapping = st.file_uploader(
                "Upload Mappings File",
                type=['json', 'xlsx', 'xls', 'csv'],
                key="mapping_uploader",
                help="JSON, Excel, or CSV file with Excel_Header → Variable_Name mappings"
            )
            
            if uploaded_mapping and not has_mappings:
                file_extension = Path(uploaded_mapping.name).suffix.lower()
                try:
                    if file_extension == '.json':
                        imported_mappings = import_mappings_from_json(uploaded_mapping)
                    elif file_extension == '.csv':
                        df_temp = pd.read_csv(uploaded_mapping)
                        if 'Excel_Header' not in df_temp.columns or 'Variable_Name' not in df_temp.columns:
                            raise ValueError("CSV must contain 'Excel_Header' and 'Variable_Name' columns")
                        imported_mappings = {}
                        for _, row in df_temp.iterrows():
                            header = str(row['Excel_Header']).strip()
                            var_name = str(row['Variable_Name']).strip()
                            if header and var_name and header != 'nan' and var_name != 'nan':
                                imported_mappings[header] = var_name
                    else:
                        imported_mappings = import_mappings_from_excel(uploaded_mapping)
                    
                    st.success(f"✅ Imported {len(imported_mappings)} mappings")
                    
                    if st.button("✔️ Apply Mappings", type="primary", key="apply_mappings"):
                        st.session_state.header_to_var_mapping = imported_mappings
                        st.rerun()
                
                except Exception as e:
                    st.error(f"❌ Error: {str(e)}")
        
        # === FORMULAS UPLOAD ===
        with col_upload2:
            st.markdown("### 🧮 Formulas")
            if has_formulas:
                st.success(f"✅ {len(st.session_state.formulas)} formulas loaded")
            
            uploaded_formulas = st.file_uploader(
                "Upload Formulas File",
                type=['json', 'xlsx', 'xls', 'csv'],
                key="formulas_uploader",
                help="JSON, Excel, or CSV file with formula definitions"
            )
            
            if uploaded_formulas and not has_formulas:
                file_extension = Path(uploaded_formulas.name).suffix.lower()
                try:
                    if file_extension == '.json':
                        imported_formulas = import_formulas_from_json(uploaded_formulas)
                    elif file_extension == '.csv':
                        df_temp = pd.read_csv(uploaded_formulas)
                        if 'formula_name' not in df_temp.columns or 'formula_expression' not in df_temp.columns:
                            raise ValueError("CSV must contain 'formula_name' and 'formula_expression' columns")
                        imported_formulas = []
                        for _, row in df_temp.iterrows():
                            formula_name = str(row['formula_name']).strip()
                            formula_expr = str(row['formula_expression']).strip()
                            if formula_name and formula_expr and formula_name != 'nan' and formula_expr != 'nan':
                                formula_dict = {
                                    'formula_name': formula_name,
                                    'formula_expression': formula_expr
                                }
                                if 'description' in df_temp.columns and pd.notna(row.get('description')):
                                    formula_dict['description'] = str(row['description']).strip()
                                if 'variables_used' in df_temp.columns and pd.notna(row.get('variables_used')):
                                    formula_dict['variables_used'] = str(row['variables_used']).strip()
                                imported_formulas.append(formula_dict)
                    else:
                        imported_formulas = import_formulas_from_excel(uploaded_formulas)
                    
                    st.success(f"✅ Imported {len(imported_formulas)} formulas")
                    
                    if st.button("✔️ Apply Formulas", type="primary", key="apply_formulas"):
                        st.session_state.formulas = imported_formulas
                        st.rerun()
                
                except Exception as e:
                    st.error(f"❌ Error: {str(e)}")
        
        st.markdown("---")
        
        # Show current status
        status_col1, status_col2 = st.columns(2)
        with status_col1:
            if has_mappings:
                st.success(f"✅ Mappings: {len(st.session_state.header_to_var_mapping)} loaded")
            else:
                st.warning("⚠️ Mappings: Not loaded")
        
        with status_col2:
            if has_formulas:
                st.success(f"✅ Formulas: {len(st.session_state.formulas)} loaded")
            else:
                st.warning("⚠️ Formulas: Not loaded")
        
        # Check if we can proceed
        if not has_mappings or not has_formulas:
            return
    else:
        # Show summary when both are loaded
        st.markdown("---")
        col_sum1, col_sum2 = st.columns(2)
        
        with col_sum1:
            st.success(f"✅ **Mappings:** {len(st.session_state.header_to_var_mapping)} variables")
            with st.expander("🔍 View Mappings"):
                df_current = pd.DataFrame([
                    {"Excel_Header": k, "Variable_Name": v}
                    for k, v in st.session_state.header_to_var_mapping.items()
                ])
                st.dataframe(df_current, use_container_width=True)
        
        with col_sum2:
            st.success(f"✅ **Formulas:** {len(st.session_state.formulas)} loaded")
            with st.expander("🔍 View Formulas"):
                for i, formula in enumerate(st.session_state.formulas, 1):
                    st.markdown(f"**{i}. {formula.get('formula_name', 'Unknown')}**")
                    st.code(formula.get('formula_expression', ''))
    
    st.markdown("---")
    
    # Check for required session state
    if 'excel_df' not in st.session_state or st.session_state.excel_df is None:
        st.warning("⚠️ No data file found in session.")
        
        # Allow uploading data file here
        st.markdown("---")
        st.subheader("📊 Upload Data File")
        
        uploaded_data_file = st.file_uploader(
            "Upload your data file (Excel, CSV, or JSON)",
            type=['csv', 'xlsx', 'xls', 'json'],
            key="data_file_uploader",
            help="Upload the file containing the data you want to calculate on"
        )
        
        if uploaded_data_file:
            file_extension = Path(uploaded_data_file.name).suffix.lower()
            try:
                if file_extension == '.csv':
                    data_df = pd.read_csv(uploaded_data_file)
                elif file_extension == '.json':
                    data_df = pd.read_json(uploaded_data_file)
                else:
                    data_df = pd.read_excel(uploaded_data_file)
                
                st.success(f"✅ Loaded {len(data_df)} rows, {len(data_df.columns)} columns")
                
                with st.expander("📊 Preview Data"):
                    st.dataframe(data_df.head(), use_container_width=True)
                
                if st.button("✔️ Use This Data File", type="primary"):
                    st.session_state.excel_df = data_df
                    st.rerun()
            
            except Exception as e:
                st.error(f"❌ Error loading file: {e}")
        
        return
    
    # === REST OF THE CALCULATION ENGINE ===
    
    # Option to reupload or use existing
    st.subheader("📊 Data Source")
    
    col_file1, col_file2 = st.columns([2, 1])
    
    with col_file1:
        use_existing = st.checkbox("Use previously uploaded data file", value=True)
    
    calc_df = None
    
    if not use_existing:
        st.markdown("### Upload New Data File")
        uploaded_calc_file = st.file_uploader(
            "Upload data file (Excel, CSV, or JSON)",
            type=['csv', 'xlsx', 'xls', 'json'],
            key="calc_data_uploader"
        )
        
        if uploaded_calc_file:
            file_extension = Path(uploaded_calc_file.name).suffix.lower()
            try:
                if file_extension == '.csv':
                    calc_df = pd.read_csv(uploaded_calc_file)
                elif file_extension == '.json':
                    calc_df = pd.read_json(uploaded_calc_file)
                else:
                    calc_df = pd.read_excel(uploaded_calc_file)
                st.success(f"✅ Loaded {len(calc_df)} rows with {len(calc_df.columns)} columns")
            except Exception as e:
                st.error(f"Error loading file: {e}")
    else:
        calc_df = st.session_state.excel_df
        st.success(f"Using file: {len(calc_df)} rows, {len(calc_df.columns)} columns")
    
    if calc_df is not None:
        # Show preview
        with st.expander("📊 Data Preview"):
            st.dataframe(calc_df.head(), use_container_width=True)
        
        st.markdown("---")
        
        # Select output columns
        st.subheader("🎯 Select Output Columns")
        
        available_cols = calc_df.columns.tolist()
        
        # Suggest columns based on formula names
        formula_names = [f.get('formula_name', '') for f in st.session_state.formulas]
        suggested_cols = []
        
        for col in available_cols:
            for fname in formula_names:
                if fname.lower() in col.lower() or col.lower() in fname.lower():
                    if col not in suggested_cols:
                        suggested_cols.append(col)
        
        selected_output_cols = st.multiselect(
            "Choose columns to fill (leave empty to create new columns)",
            options=available_cols,
            default=suggested_cols[:5] if suggested_cols else [],
            help="Select columns where formula results will be written"
        )
        
        if not selected_output_cols:
            st.info("ℹ️ New columns will be created for each formula")
        
        st.markdown("---")
        
        # Run calculations button
        col_btn1, col_btn2, col_btn3 = st.columns([1, 1, 2])
        
        with col_btn1:
            if st.button("▶️ Run Calculations", type="primary"):
                with st.spinner("Running calculations..."):
                    formulas_to_run = st.session_state.formulas
                    
                    try:
                        result_df, calc_results = run_calculations(
                            calc_df,
                            formulas_to_run,
                            st.session_state.header_to_var_mapping,
                            selected_output_cols
                        )
                        
                        st.session_state.results_df = result_df
                        st.session_state.calc_results = calc_results
                        
                        st.success("✅ Calculations complete!")
                        st.rerun()
                    
                    except Exception as e:
                        st.error(f"❌ Calculation Error: {e}")
                        import traceback
                        st.code(traceback.format_exc())
        
        with col_btn2:
            if st.button("🔄 Reset Results"):
                if 'results_df' in st.session_state:
                    del st.session_state.results_df
                if 'calc_results' in st.session_state:
                    del st.session_state.calc_results
                st.rerun()
    
    # Display Results if they exist
    if 'results_df' in st.session_state and st.session_state.results_df is not None:
        st.markdown("---")
        st.subheader("✅ Calculation Results")
        
        # Summary statistics
        col1, col2, col3 = st.columns(3)
        
        total_rows = len(st.session_state.results_df)
        total_formulas = len(st.session_state.calc_results)
        
        avg_success = 0
        if total_formulas > 0:
            avg_success = sum(r.success_rate for r in st.session_state.calc_results) / total_formulas
        
        with col1:
            st.metric("Total Rows", total_rows)
        with col2:
            st.metric("Formulas Applied", total_formulas)
        with col3:
            st.metric("Avg Success Rate", f"{avg_success:.1f}%")
        
        # Show detailed results
        st.markdown("---")
        st.markdown("### Formula Results")
        
        for calc_result in st.session_state.calc_results:
            status_icon = "✅" if calc_result.success_rate >= 90 else "⚠️" if calc_result.success_rate >= 50 else "❌"
            
            with st.expander(f"{status_icon} **{calc_result.formula_name}** - {calc_result.success_rate:.1f}% success"):
                st.markdown(f"**Rows Calculated:** {calc_result.rows_calculated} / {total_rows}")
                
                if calc_result.errors:
                    st.markdown(f"**Errors ({len(calc_result.errors)} shown):**")
                    for error in calc_result.errors:
                        st.error(error)
        
        # Show results dataframe
        st.markdown("---")
        st.markdown("### Results Data")
        
        # Show statistics for calculated columns
        calculated_cols = [col for col in st.session_state.results_df.columns 
                          if col not in calc_df.columns or col in (selected_output_cols if selected_output_cols else [])]
        
        if calculated_cols:
            st.write("**Calculated Column Statistics:**")
            for col in calculated_cols:
                if col in st.session_state.results_df.columns:
                    non_null_count = st.session_state.results_df[col].notna().sum()
                    st.write(f"- **{col}**: {non_null_count} non-null values out of {total_rows}")
                    if non_null_count > 0:
                        sample_values = st.session_state.results_df[col].dropna().head(3).tolist()
                        st.write(f"  Sample values: {sample_values}")
        
        st.dataframe(st.session_state.results_df, use_container_width=True, height=400)
        
        # Export options
        st.markdown("---")
        col_exp1, col_exp2, col_exp3 = st.columns([1, 1, 2])
        
        with col_exp1:
            from io import BytesIO
            output = BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                st.session_state.results_df.to_excel(writer, index=False, sheet_name='Results')
            
            st.download_button(
                label="📥 Download Excel",
                data=output.getvalue(),
                file_name="calculation_results.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        
        with col_exp2:
            csv_data = st.session_state.results_df.to_csv(index=False)
            st.download_button(
                label="📥 Download CSV",
                data=csv_data,
                file_name="calculation_results.csv",
                mime="text/csv"
            )

if __name__ == "__main__":
    main()