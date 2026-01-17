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
from dateutil.relativedelta import relativedelta

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

# --- Derived Formulas ---
BASIC_DERIVED_FORMULAS = {
    'no_of_premium_paid': {
        'description': 'Number of premiums paid based on term start and FUP date',
        'formula': 'MONTHS_BETWEEN(FUP_Date, TERM_START_DATE) / 12',
        'variables': ['FUP_Date', 'TERM_START_DATE']
    },
    'policy_year': {
        'description': 'Policy year based on term start and surrender date',
        'formula': 'MONTHS_BETWEEN(DATE_OF_SURRENDER, TERM_START_DATE) / 12 + 1',
        'variables': ['DATE_OF_SURRENDER', 'TERM_START_DATE']
    },
    'maturity_date': {
        'description': 'Maturity date calculation',
        'formula': 'ADD_MONTHS(TERM_START_DATE, BENEFIT_TERM * 12)',
        'variables': ['TERM_START_DATE', 'BENEFIT_TERM']
    },
    'Elapsed_policy_duration': {
        'description': 'Years elapsed since policy start',
        'formula': 'MONTHS_BETWEEN(CURRENT_DATE, TERM_START_DATE) / 12',
        'variables': ['TERM_START_DATE']
    }
}

def get_derived_formulas() -> List[Dict]:
    """Convert derived formulas to standard formula format"""
    formulas = []
    for name, info in BASIC_DERIVED_FORMULAS.items():
        formulas.append({
            'formula_name': name,
            'formula_expression': info['formula'],
            'description': info['description'],
            'variables_used': ', '.join(info['variables'])
        })
    return formulas

# --- Helper Functions ---
def safe_convert_to_number(value: Any) -> float:
    """Safely convert various types to float, handling dates, timestamps, etc."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0.0
    
    if isinstance(value, str) and (value == '' or value.strip() == ''):
        return 0.0
    
    if isinstance(value, (int, float)) and not pd.isna(value):
        return float(value)
    
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return float(value.year)
    
    if isinstance(value, str):
        try:
            cleaned = value.replace(',', '').replace('$', '').replace('%', '').strip()
            if cleaned:
                return float(cleaned)
            return 0.0
        except ValueError:
            try:
                parsed_date = pd.to_datetime(value)
                return float(parsed_date.year)
            except:
                return 0.0
    
    return 0.0

def months_between(date1, date2):
    """Calculate months between two dates"""
    try:
        if pd.isna(date1) or pd.isna(date2):
            return 0
        
        d1 = pd.to_datetime(date1)
        d2 = pd.to_datetime(date2)
        
        months = (d2.year - d1.year) * 12 + (d2.month - d1.month)
        return float(months)
    except:
        return 0

def add_months(date, months):
    """Add months to a date"""
    try:
        if pd.isna(date):
            return None
        
        d = pd.to_datetime(date)
        result = d + relativedelta(months=int(months))
        return result
    except:
        return None

def safe_eval(expression: str, variables: Dict[str, Any]) -> Any:
    """Safely evaluate a mathematical expression with given variables"""
    try:
        eval_expr = expression.strip()
        
        # Remove variable assignment if present
        if '=' in eval_expr and not any(op in eval_expr for op in ['==', '!=', '<=', '>=']):
            parts = eval_expr.split('=', 1)
            if len(parts) == 2:
                left_side = parts[0].strip()
                if left_side.replace('_', '').replace(' ', '').isalnum():
                    eval_expr = parts[1].strip()
        
        # Handle special date functions
        if 'MONTHS_BETWEEN' in eval_expr.upper():
            pattern = r'MONTHS_BETWEEN\s*\(\s*([^,]+)\s*,\s*([^)]+)\s*\)'
            matches = re.finditer(pattern, eval_expr, re.IGNORECASE)
            for match in matches:
                var1, var2 = match.group(1).strip(), match.group(2).strip()
                val1 = variables.get(var1, 0)
                val2 = variables.get(var2, 0)
                result = months_between(val1, val2)
                eval_expr = eval_expr.replace(match.group(0), str(result))
        
        if 'ADD_MONTHS' in eval_expr.upper():
            pattern = r'ADD_MONTHS\s*\(\s*([^,]+)\s*,\s*([^)]+)\s*\)'
            matches = re.finditer(pattern, eval_expr, re.IGNORECASE)
            for match in matches:
                var1, var2 = match.group(1).strip(), match.group(2).strip()
                
                # Evaluate var2 if it's an expression
                if any(op in var2 for op in ['+', '-', '*', '/']):
                    for var_name in sorted(variables.keys(), key=len, reverse=True):
                        var2 = re.sub(r'\b' + re.escape(var_name) + r'\b', 
                                     str(safe_convert_to_number(variables[var_name])), 
                                     var2, flags=re.IGNORECASE)
                    try:
                        months_val = eval(var2, {"__builtins__": {}}, {})
                    except:
                        months_val = 0
                else:
                    months_val = safe_convert_to_number(variables.get(var2, 0))
                
                val1 = variables.get(var1, 0)
                result = add_months(val1, months_val)
                
                if result:
                    return result
                else:
                    eval_expr = eval_expr.replace(match.group(0), '0')
        
        if 'CURRENT_DATE' in eval_expr.upper():
            current_date = datetime.now()
            eval_expr = re.sub(r'\bCURRENT_DATE\b', f"'{current_date.strftime('%Y-%m-%d')}'", 
                              eval_expr, flags=re.IGNORECASE)
        
        # Excel function mappings
        func_mappings = {
            r'\bMAX\s*\(': 'max(',
            r'\bMIN\s*\(': 'min(',
            r'\bABS\s*\(': 'abs(',
            r'\bROUND\s*\(': 'round(',
            r'\bPOWER\s*\(': 'pow(',
            r'\bSQRT\s*\(': 'math.sqrt(',
            r'\bSUM\s*\(': 'sum(',
        }
        
        for pattern, replacement in func_mappings.items():
            eval_expr = re.sub(pattern, replacement, eval_expr, flags=re.IGNORECASE)
        
        # Replace variables (case insensitive, longest first)
        sorted_vars = sorted(variables.keys(), key=len, reverse=True)
        
        for var_name in sorted_vars:
            value = variables[var_name]
            numeric_value = safe_convert_to_number(value)
            pattern = r'\b' + re.escape(var_name) + r'\b'
            eval_expr = re.sub(pattern, str(numeric_value), eval_expr, flags=re.IGNORECASE)
        
        # Safe evaluation
        allowed_builtins = {
            'max': max, 'min': min, 'abs': abs, 'round': round,
            'int': int, 'float': float, 'pow': pow, 'sum': sum, 'len': len
        }
        
        result = eval(eval_expr, {"__builtins__": allowed_builtins, "math": math}, {})
        
        if isinstance(result, (int, float)) and not (isinstance(result, float) and (math.isnan(result) or math.isinf(result))):
            return float(result)
        else:
            return None
    
    except Exception as e:
        print(f"Evaluation error for '{expression}': {str(e)}")
        return None

def calculate_row(row: pd.Series, formula_expr: str, header_to_var_mapping: Dict[str, str]) -> Any:
    """Calculate formula result for a single row"""
    var_values = {}
    
    # Reverse mapping: variable_name -> excel_header
    var_to_header = {v: k for k, v in header_to_var_mapping.items()}
    
    # Build variable values
    for var_name, header in var_to_header.items():
        if header in row.index:
            var_values[var_name] = row[header]
        else:
            # Try case-insensitive match
            for col in row.index:
                if col.lower() == header.lower():
                    var_values[var_name] = row[col]
                    break
    
    result = safe_eval(formula_expr, var_values)
    return result

def find_matching_column(formula_name: str, df_columns: List[str], header_to_var_mapping: Dict[str, str]) -> str:
    """Find the best matching column for a formula"""
    formula_lower = formula_name.lower().replace('_', '').replace(' ', '')
    
    # Check if formula_name is already a variable that's mapped
    var_to_header = {v: k for k, v in header_to_var_mapping.items()}
    if formula_name in var_to_header:
        return var_to_header[formula_name]
    
    # Check exact match in columns
    for col in df_columns:
        col_clean = col.lower().replace('_', '').replace(' ', '')
        if col_clean == formula_lower:
            return col
    
    # Check partial match
    for col in df_columns:
        col_lower = col.lower()
        fname_lower = formula_name.lower()
        if fname_lower in col_lower or col_lower in fname_lower:
            return col
    
    # Token matching
    formula_tokens = set(re.findall(r'\w+', formula_name.lower()))
    best_match = None
    best_score = 0
    
    for col in df_columns:
        col_tokens = set(re.findall(r'\w+', col.lower()))
        overlap = len(formula_tokens & col_tokens)
        if overlap > best_score:
            best_score = overlap
            best_match = col
    
    if best_match and best_score > 0:
        return best_match
    
    return formula_name

def run_calculations(df: pd.DataFrame, 
                     formulas: List[Dict], 
                     header_to_var_mapping: Dict[str, str],
                     include_derived: bool = True) -> tuple[pd.DataFrame, List[CalculationResult]]:
    """Run formulas on dataframe"""
    result_df = df.copy()
    calculation_results = []
    
    # Add derived formulas if requested
    all_formulas = formulas.copy()
    if include_derived:
        derived = get_derived_formulas()
        all_formulas.extend(derived)
        st.info(f"📊 Added {len(derived)} derived formulas to calculation queue")
    
    st.info(f"🔧 Processing {len(all_formulas)} total formulas")
    
    df_columns = df.columns.tolist()
    
    for formula in all_formulas:
        formula_name = formula.get('formula_name', 'Unknown')
        formula_expr = formula.get('formula_expression', '')
        
        # Find matching column
        output_col = find_matching_column(formula_name, df_columns, header_to_var_mapping)
        
        # Check if column exists
        col_existed = output_col in result_df.columns
        
        if not col_existed:
            result_df[output_col] = np.nan
            st.info(f"📝 **{formula_name}** → Creating new column: **{output_col}**")
        else:
            st.info(f"✏️ **{formula_name}** → Writing to existing: **{output_col}**")
        
        st.code(f"Expression: {formula_expr}", language="python")
        
        errors = []
        success_count = 0
        total_rows = len(result_df)
        
        # Debug first row
        if total_rows > 0:
            first_row = result_df.iloc[0]
            var_to_header = {v: k for k, v in header_to_var_mapping.items()}
            var_values_debug = {}
            
            for var_name, header in var_to_header.items():
                if header in first_row.index:
                    var_values_debug[var_name] = first_row[header]
            
            with st.expander(f"🔍 Debug: {formula_name}"):
                st.write(f"**Available variables:** {len(var_values_debug)}")
                
                # Show first 5 variable mappings
                sample_vars = dict(list(var_values_debug.items())[:5])
                for var, val in sample_vars.items():
                    st.write(f"  - `{var}` = {val} (type: {type(val).__name__})")
                
                first_result = calculate_row(first_row, formula_expr, header_to_var_mapping)
                st.write(f"**Test Result:** {first_result}")
                
                if first_result is None:
                    st.error("⚠️ Test calculation returned None - check formula and variables")
        
        # Progress tracking
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        # Calculate for all rows
        for idx in range(len(result_df)):
            try:
                row = result_df.iloc[idx]
                result = calculate_row(row, formula_expr, header_to_var_mapping)
                
                if result is None:
                    if idx < 5:  # Only log first 5 errors
                        errors.append(f"Row {idx}: Calculation returned None")
                    result_df.at[result_df.index[idx], output_col] = np.nan
                else:
                    result_df.at[result_df.index[idx], output_col] = result
                    success_count += 1
            
            except Exception as e:
                if idx < 5:
                    errors.append(f"Row {idx}: {str(e)}")
                result_df.at[result_df.index[idx], output_col] = np.nan
            
            if idx % 10 == 0 or idx == total_rows - 1:
                progress = (idx + 1) / total_rows
                progress_bar.progress(progress)
                status_text.text(f"Processing {formula_name}: {idx+1}/{total_rows}")
        
        progress_bar.empty()
        status_text.empty()
        
        success_rate = (success_count / total_rows * 100) if total_rows > 0 else 0
        non_null_count = result_df[output_col].notna().sum()
        
        status_icon = "✅" if success_rate >= 90 else "⚠️" if success_rate >= 50 else "❌"
        st.success(f"{status_icon} **{formula_name}**: {success_count}/{total_rows} rows ({success_rate:.1f}% success)")
        
        if non_null_count > 0:
            sample_vals = result_df[output_col].dropna().head(3).tolist()
            st.write(f"Sample values: {sample_vals}")
        
        calculation_results.append(CalculationResult(
            formula_name=f"{formula_name} → {output_col}",
            rows_calculated=success_count,
            errors=errors[:10],
            success_rate=success_rate
        ))
        
        st.markdown("---")
    
    return result_df, calculation_results

# --- Import Functions ---
def import_mappings_from_json(json_file) -> Dict[str, str]:
    """Import mappings from JSON file"""
    try:
        content = json_file.read()
        mappings = json.loads(content)
        
        if not isinstance(mappings, dict):
            raise ValueError("JSON must be a dictionary")
        
        clean_mappings = {}
        for k, v in mappings.items():
            header = str(k).strip()
            var_name = str(v).strip()
            if header and var_name and header != 'nan' and var_name != 'nan':
                clean_mappings[header] = var_name
        
        return clean_mappings
    except Exception as e:
        raise ValueError(f"Error reading JSON: {str(e)}")

def import_formulas_from_json(json_file) -> List[Dict]:
    """Import formulas from JSON file"""
    try:
        content = json_file.read()
        data = json.loads(content)
        
        if isinstance(data, dict) and 'formulas' in data:
            formulas = data['formulas']
            st.info(f"📊 Extraction format detected")
        elif isinstance(data, list):
            formulas = data
        else:
            raise ValueError("Invalid format")
        
        validated_formulas = []
        for i, formula in enumerate(formulas):
            if 'formula_name' not in formula or 'formula_expression' not in formula:
                continue
            
            expr = formula['formula_expression'].strip().strip('[]')
            
            validated_formulas.append({
                'formula_name': formula['formula_name'],
                'formula_expression': expr,
                'description': formula.get('description', ''),
                'variables_used': formula.get('variables_used', '')
            })
        
        return validated_formulas
    except Exception as e:
        raise ValueError(f"Error reading JSON: {str(e)}")

# --- Main App ---
def main():
    st.set_page_config(page_title="Calculation Engine", page_icon="🧮", layout="wide")
    load_css()
    
    st.markdown("""
        <div class="header-container">
            <div class="header-bar">
                <img src="https://raw.githubusercontent.com/AyushiR0y/streamlit_formulagen/main/assets/logo.png" style="height: 100px;">
                <div class="header-title" style="font-size: 2.5rem; font-weight: 750; color: #004DA8;">
                    Calculation Engine
                </div>
            </div>
        </div>
    """, unsafe_allow_html=True)
    
    st.markdown("---")
    
    # Check prerequisites
    has_mappings = 'header_to_var_mapping' in st.session_state and st.session_state.header_to_var_mapping
    has_formulas = 'formulas' in st.session_state and st.session_state.formulas
    has_data = 'excel_df' in st.session_state and st.session_state.excel_df is not None
    
    # Upload section if missing
    if not has_mappings or not has_formulas:
        st.warning("⚠️ Missing configuration files")
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.markdown("### 📋 Variable Mappings")
            if has_mappings:
                st.success(f"✅ {len(st.session_state.header_to_var_mapping)} loaded")
            
            uploaded_mapping = st.file_uploader("Upload Mappings (JSON)", type=['json'], key="map_up")
            if uploaded_mapping and not has_mappings:
                try:
                    imported_mappings = import_mappings_from_json(uploaded_mapping)
                    st.success(f"✅ {len(imported_mappings)} mappings")
                    if st.button("Apply Mappings", type="primary"):
                        st.session_state.header_to_var_mapping = imported_mappings
                        st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")
        
        with col2:
            st.markdown("### 🧮 Formulas")
            if has_formulas:
                st.success(f"✅ {len(st.session_state.formulas)} loaded")
            
            uploaded_formulas = st.file_uploader("Upload Formulas (JSON)", type=['json'], key="form_up")
            if uploaded_formulas and not has_formulas:
                try:
                    imported_formulas = import_formulas_from_json(uploaded_formulas)
                    st.success(f"✅ {len(imported_formulas)} formulas")
                    if st.button("Apply Formulas", type="primary"):
                        st.session_state.formulas = imported_formulas
                        st.rerun()
                except Exception as e:
                    st.error(f"Error: {e}")
        
        if not has_mappings or not has_formulas:
            return
    else:
        col1, col2 = st.columns(2)
        with col1:
            st.success(f"✅ **Mappings:** {len(st.session_state.header_to_var_mapping)}")
        with col2:
            st.success(f"✅ **Formulas:** {len(st.session_state.formulas)}")
    
    st.markdown("---")
    
    # Data file handling
    if not has_data:
        st.warning("⚠️ No data file loaded")
        uploaded_data = st.file_uploader("Upload Data File", type=['csv', 'xlsx', 'xls', 'json'])
        
        if uploaded_data:
            try:
                ext = Path(uploaded_data.name).suffix.lower()
                if ext == '.csv':
                    df = pd.read_csv(uploaded_data)
                elif ext == '.json':
                    df = pd.read_json(uploaded_data)
                else:
                    df = pd.read_excel(uploaded_data)
                
                st.success(f"✅ {len(df)} rows, {len(df.columns)} columns")
                if st.button("Use This Data", type="primary"):
                    st.session_state.excel_df = df
                    st.rerun()
            except Exception as e:
                st.error(f"Error: {e}")
        return
    
    calc_df = st.session_state.excel_df
    st.success(f"📊 Data: {len(calc_df)} rows, {len(calc_df.columns)} columns")
    
    with st.expander("📊 Data Preview"):
        st.dataframe(calc_df.head(10), use_container_width=True)
    
    st.markdown("---")
    
    # Options
    include_derived = st.checkbox("Include derived formulas", value=True, 
                                  help="Automatically include standard derived calculations")
    
    if include_derived:
        with st.expander("📋 Derived Formulas"):
            for name, info in BASIC_DERIVED_FORMULAS.items():
                st.write(f"**{name}**: {info['description']}")
                st.code(info['formula'])
    
    st.markdown("---")
    
    # Run calculations
    col1, col2 = st.columns([1, 3])
    with col1:
        if st.button("▶️ Run Calculations", type="primary", use_container_width=True):
            with st.spinner("Calculating..."):
                try:
                    result_df, calc_results = run_calculations(
                        calc_df,
                        st.session_state.formulas,
                        st.session_state.header_to_var_mapping,
                        include_derived
                    )
                    
                    st.session_state.results_df = result_df
                    st.session_state.calc_results = calc_results
                    st.success("✅ Calculations complete!")
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ Error: {e}")
                    import traceback
                    st.code(traceback.format_exc())
    
    with col2:
        if st.button("🔄 Reset Results"):
            if 'results_df' in st.session_state:
                del st.session_state.results_df
            if 'calc_results' in st.session_state:
                del st.session_state.calc_results
            st.rerun()
    
    # Display results
    if 'results_df' in st.session_state and st.session_state.results_df is not None:
        st.markdown("---")
        st.subheader("✅ Results")
        
        total_rows = len(st.session_state.results_df)
        total_formulas = len(st.session_state.calc_results)
        avg_success = sum(r.success_rate for r in st.session_state.calc_results) / total_formulas if total_formulas > 0 else 0
        
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Total Rows", total_rows)
        with col2:
            st.metric("Formulas Applied", total_formulas)
        with col3:
            st.metric("Avg Success", f"{avg_success:.1f}%")
        
        st.markdown("---")
        
        with st.expander("📊 Formula Details", expanded=False):
            for calc_result in st.session_state.calc_results:
                icon = "✅" if calc_result.success_rate >= 90 else "⚠️" if calc_result.success_rate >= 50 else "❌"
                st.write(f"{icon} **{calc_result.formula_name}**: {calc_result.success_rate:.1f}%")
                if calc_result.errors:
                    for err in calc_result.errors[:3]:
                        st.caption(f"  {err}")
        
        st.dataframe(st.session_state.results_df, use_container_width=True, height=400)
        
        # Export
        from io import BytesIO
        col1, col2 = st.columns(2)
        
        with col1:
            output = BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                st.session_state.results_df.to_excel(writer, index=False)
            st.download_button("📥 Download Excel", output.getvalue(), 
                             "results.xlsx", 
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        
        with col2:
            csv = st.session_state.results_df.to_csv(index=False)
            st.download_button("📥 Download CSV", csv, "results.csv", "text/csv")

if __name__ == "__main__":
    main()