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
    """Loads CSS file."""
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
        # Swapped arguments to ensure positive duration usually, or rely on abs logic below
        'formula': 'MONTHS_BETWEEN(TERM_START_DATE, FUP_Date)', 
        'variables': ['FUP_Date', 'TERM_START_DATE']
    },
    'policy_year': {
        'description': 'Policy year based on term start and surrender date',
        'formula': 'MONTHS_BETWEEN(TERM_START_DATE, DATE_OF_SURRENDER) / 12 + 1',
        'variables': ['DATE_OF_SURRENDER', 'TERM_START_DATE']
    },
    'maturity_date': {
        'description': 'Maturity date calculation',
        'formula': 'ADD_MONTHS(TERM_START_DATE, BENEFIT_TERM * 12)',
        'variables': ['TERM_START_DATE', 'BENEFIT_TERM']
    },
    'Elapsed_policy_duration': {
        'description': 'Years elapsed since policy start',
        'formula': 'MONTHS_BETWEEN(TERM_START_DATE, CURRENT_DATE) / 12',
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
            'variables_used': ', '.join(info['variables']),
            'is_pre_mapped': False # Derived formulas use variables, not direct headers
        })
    return formulas

# --- Helper Functions ---
def safe_convert_to_number(value: Any) -> float:
    """Safely convert various types to float"""
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
    """Calculate absolute months between two dates"""
    try:
        if pd.isna(date1) or pd.isna(date2):
            return 0
        
        d1 = pd.to_datetime(date1)
        d2 = pd.to_datetime(date2)
        
        # Calculate absolute difference to prevent negative results if args swapped
        months = abs((d2.year - d1.year) * 12 + (d2.month - d1.month))
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
    """Safely evaluate a mathematical expression"""
    try:
        eval_expr = expression.strip()

        # 0. Handle Excel-style assignment expressions: "VAR = expr" -> "expr"
        # Some extracted formulas come as "SSV3_AMT = Income_Benefit_Amount * SSV3_FACTOR"
        # We only evaluate the RHS.
        if '=' in eval_expr:
            # Avoid breaking comparisons (not currently supported anyway), only strip single assignment style.
            # Take the last '=' to be tolerant of "A = B = C" (use RHS-most).
            parts = eval_expr.split('=')
            if len(parts) >= 2:
                eval_expr = parts[-1].strip()

        # 0b. Handle Excel-style percent literals in expressions, e.g. "105%" or "1.05%"
        # Convert "105%" -> "(105/100)" so downstream eval works.
        eval_expr = re.sub(
            r'(?<![a-zA-Z0-9_])(\d+(?:\.\d+)?)\s*%',
            r'(\1/100)',
            eval_expr
        )
        
        # 1. Handle special date functions BEFORE variable replacement
        if 'MONTHS_BETWEEN' in eval_expr.upper():
            pattern = r'MONTHS_BETWEEN\s*\(\s*([^,]+)\s*,\s*([^)]+)\s*\)'
            matches = re.finditer(pattern, eval_expr, re.IGNORECASE)
            for match in matches:
                var1, var2 = match.group(1).strip(), match.group(2).strip()
                # These might be variables or direct values
                val1 = variables.get(var1, var1)
                val2 = variables.get(var2, var2)
                result = months_between(val1, val2)
                eval_expr = eval_expr.replace(match.group(0), str(result))
        
        if 'ADD_MONTHS' in eval_expr.upper():
            pattern = r'ADD_MONTHS\s*\(\s*([^,]+)\s*,\s*([^)]+)\s*\)'
            matches = re.finditer(pattern, eval_expr, re.IGNORECASE)
            for match in matches:
                var1, var2 = match.group(1).strip(), match.group(2).strip()
                val1 = variables.get(var1, var1)
                # Evaluate var2 if it's an expression
                try:
                    # Check if var2 is a number or needs eval
                    if any(op in var2 for op in ['+', '-', '*', '/']):
                         # Use the variables dict to resolve sub-expressions if possible
                         # But safe here to just try converting to number first
                         val2_float = float(re.sub(r'[^0-9\.\-\+*/eE]', '', str(var2)))
                    else:
                        val2_float = float(var2)
                except:
                    val2_float = 0
                
                result = add_months(val1, val2_float)
                
                if result:
                    return result # ADD_MONTHS usually returns a date, returning immediately
                else:
                    eval_expr = eval_expr.replace(match.group(0), '0')
        
        if 'CURRENT_DATE' in eval_expr.upper():
            current_date = datetime.now()
            eval_expr = re.sub(r'\bCURRENT_DATE\b', f"'{current_date.strftime('%Y-%m-%d')}'", 
                              eval_expr, flags=re.IGNORECASE)
        
        # 2. Excel function mappings
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
        
        # 3. Replace variables
        # Sort by length (descending) to avoid partial replacements (e.g., "TOTAL_PREM" before "PREM")
        # We assume keys in 'variables' are exact matches needed in the formula (with or without brackets)
        sorted_vars = sorted(variables.keys(), key=len, reverse=True)
        
        for var_name in sorted_vars:
            value = variables[var_name]
            numeric_value = safe_convert_to_number(value)
            
            # Handle bracketed keys specifically for regex safety, or use raw match
            if var_name.startswith('[') and var_name.endswith(']'):
                # Exact string replacement for brackets
                eval_expr = eval_expr.replace(var_name, str(numeric_value))
            else:
                # Word boundary for standard variables
                pattern = r'\b' + re.escape(var_name) + r'\b'
                eval_expr = re.sub(pattern, str(numeric_value), eval_expr, flags=re.IGNORECASE)
        
        # 4. Safe evaluation
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
        print(f"Evaluation error: {e} | Expr: {expression}")
        return None

def calculate_row(row: pd.Series, formula_expr: str, header_to_var_mapping: Dict[str, str], is_pre_mapped: bool = False) -> Any:
    """
    Calculate formula result for a single row.
    HYBRID LOGIC: Handles [Bracketed Headers], Existing Columns (results from previous formulas), and Standard Variables.
    """
    var_values = {}
    
    # 1. EXTRACT Bracketed Headers [Name] for Pre-Mapped formulas
    bracketed_headers = set()
    if is_pre_mapped:
        pattern = r'\[([^\]]+)\]'
        matches = re.findall(pattern, formula_expr)
        bracketed_headers.update(matches)
        
        # Populate var_values with bracketed headers
        for header_name in bracketed_headers:
            # Lookup in row (case insensitive)
            val = None
            # Try exact first
            if header_name in row.index:
                val = row[header_name]
            else:
                # Try case insensitive
                for col in row.index:
                    if col.lower() == header_name.lower():
                        val = row[col]
                        break
            
            # Store with brackets included to ensure safe_eval replaces the exact token
            if val is not None:
                var_values[f"[{header_name}]"] = val
            else:
                # If missing, treat as 0 or None? 0 might mask errors, but keeps calculation running
                var_values[f"[{header_name}]"] = 0.0

    # 2. IDENTIFY Potential Variables (Non-bracketed tokens)
    # We will check against:
    #   a) Existing Columns in the DataFrame (e.g. results of derived formulas run previously)
    #   b) The header_to_var_mapping (for standard inputs)
    
    # Remove bracketed parts temporarily to find other tokens
    # Simple tokenization: remove known functions and brackets
    clean_expr = re.sub(r'\[[^\]]+\]', '', formula_expr) # Remove headers
    clean_expr = re.sub(r'MONTHS_BETWEEN\([^)]+\)', '', clean_expr, flags=re.IGNORECASE)
    clean_expr = re.sub(r'ADD_MONTHS\([^)]+\)', '', clean_expr, flags=re.IGNORECASE)
    
    potential_vars = set(re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', clean_expr))
    
    # Filter out Python keywords/funcs
    python_keywords = {'max', 'min', 'abs', 'round', 'sum', 'pow', 'math', 'sqrt', 'len', 'int', 'float'}
    potential_vars = potential_vars - python_keywords
    
    # 3. POPULATE var_values with found variables
    # Build reverse mapping too, because different pages/files sometimes store:
    #   - header_to_var_mapping as {ExcelHeader: Variable}
    #   - OR user uploads {Variable: ExcelHeader}
    # We'll support both by checking both directions.
    var_to_header_mapping = {v: k for k, v in header_to_var_mapping.items() if v}
    for var_name in potential_vars:
        # A) Check if it's an existing column (e.g. 'policy_year' calculated in step 1)
        if var_name in row.index:
            var_values[var_name] = row[var_name]
            continue
            
        # B) Check if it maps via header_to_var_mapping (Standard Input)
        mapped_header = None
        # Case 1: mapping stored as {Variable: ExcelHeader}
        if var_name in header_to_var_mapping and header_to_var_mapping[var_name]:
            mapped_header = header_to_var_mapping[var_name]
        # Case 2: mapping stored as {ExcelHeader: Variable}
        elif var_name in var_to_header_mapping:
            mapped_header = var_to_header_mapping[var_name]

        if mapped_header:
            if mapped_header in row.index:
                var_values[var_name] = row[mapped_header]
            else:
                # Case-insensitive match against actual df columns
                for col in row.index:
                    if col.lower() == str(mapped_header).lower():
                        var_values[var_name] = row[col]
                        break
    
    result = safe_eval(formula_expr, var_values)
    return result

def find_matching_column(formula_name: str, df_columns: List[str], header_to_var_mapping: Dict[str, str]) -> str:
    """
    Find the best matching column for a formula.
    UPDATED: 
    1. Prioritize Exact Column Name Match (Autofill).
    2. Prioritize Partial Match (Fuzzy).
    3. REMOVED 'var_to_header' lookup which was causing Policy Status to be overwritten.
    """
    formula_lower = formula_name.lower().replace('_', '').replace(' ', '')
    
    # 1. Check exact match in columns (Case Insensitive)
    for col in df_columns:
        col_clean = col.lower().replace('_', '').replace(' ', '')
        if col_clean == formula_lower:
            return col
    
    # 2. Check partial match (Formula Name contained in Column Name)
    # This ensures "TOTAL_PREMIUM_PAID" matches "Total Premium Paid" column
    for col in df_columns:
        col_lower = col.lower()
        fname_lower = formula_name.lower()
        if fname_lower in col_lower or col_lower in fname_lower:
            return col
    
    # 3. Token matching (Last resort)
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
    
    # 4. No match found -> Return formula name (Create New Column)
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
        # Ensure derived formulas are NOT marked as pre-mapped
        for f in derived: 
            f['is_pre_mapped'] = False
        all_formulas.extend(derived)
        st.info(f"📊 Added {len(derived)} derived formulas to calculation queue")
    
    st.info(f"🔧 Processing {len(all_formulas)} total formulas")
    
    df_columns = df.columns.tolist()
    
    for formula in all_formulas:
        formula_name = formula.get('formula_name', 'Unknown')
        formula_expr = formula.get('formula_expression', '')
        is_pre_mapped = formula.get('is_pre_mapped', False)
        
        # Find matching column
        output_col = find_matching_column(formula_name, df_columns, header_to_var_mapping)
        
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
            
            with st.expander(f"🔍 Debug: {formula_name}"):
                if is_pre_mapped:
                    st.write("**Mode:** Pre-Mapped (Direct Header Lookup)")
                    # Show parsed headers
                    pattern = r'\[([^\]]+)\]'
                    headers_in_formula = re.findall(pattern, formula_expr)
                    st.write(f"**Headers in brackets:** {headers_in_formula}")
                    
                    # Show values
                    for h in headers_in_formula:
                        val = first_row.get(h, "Not Found")
                        st.write(f"  - `[{h}]` = {val}")
                else:
                    st.write("**Mode:** Variable Mapping / Hybrid")
                    var_to_header = {v: k for k, v in header_to_var_mapping.items()}
                    st.write(f"**Variables used:** {formula.get('variables_used', 'Unknown')}")
                
                first_result = calculate_row(first_row, formula_expr, header_to_var_mapping, is_pre_mapped=is_pre_mapped)
                st.write(f"**Test Result:** {first_result}")
                
                if first_result is None:
                    st.error("⚠️ Test calculation returned None - check formula, headers, and variables")
        
        # Progress tracking
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        # Calculate for all rows
        for idx in range(len(result_df)):
            try:
                row = result_df.iloc[idx]
                result = calculate_row(row, formula_expr, header_to_var_mapping, is_pre_mapped=is_pre_mapped)
                
                if result is None:
                    if idx < 5:
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
    """Import formulas from JSON file. Handles both Raw and Pre-Mapped formats."""
    try:
        content = json_file.read()
        data = json.loads(content)
        
        # Handle container formats
        if isinstance(data, dict) and 'formulas' in data:
            formulas = data['formulas']
            st.info("📊 Extraction format detected")
        elif isinstance(data, list):
            formulas = data
        else:
            raise ValueError("Invalid format")
        
        validated_formulas = []
        for formula in formulas:
            # Detect Pre-Mapped Structure
            is_pre_mapped = False
            final_expression = ""
            original_expression = ""
            
            if 'mapped_expression' in formula:
                is_pre_mapped = True
                final_expression = formula['mapped_expression']
                original_expression = formula.get('original_expression', '')
            else:
                is_pre_mapped = False
                raw_expr = formula.get('formula_expression', '')
                final_expression = raw_expr.strip('[]')
                original_expression = raw_expr
            
            if not formula.get('formula_name'):
                continue
            
            validated_formulas.append({
                'formula_name': formula['formula_name'],
                'formula_expression': final_expression,
                'original_expression': original_expression,
                'is_pre_mapped': is_pre_mapped,
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
            # FIX: Generate fresh BytesIO every time to ensure file exists on download
            @st.cache_data
            def convert_df(df):
                return df.to_csv(index=False).encode('utf-8')
            
            csv = convert_df(st.session_state.results_df)
            st.download_button(
                label="📥 Download CSV",
                data=csv,
                file_name="results.csv",
                mime="text/csv"
            )
        
        with col2:
            # FIX: Generate Excel bytes fresh
            buffer = BytesIO()
            with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                st.session_state.results_df.to_excel(writer, index=False)
            
            st.download_button(
                label="📥 Download Excel",
                data=buffer.getvalue(),
                file_name="results.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

if __name__ == "__main__":
    main()