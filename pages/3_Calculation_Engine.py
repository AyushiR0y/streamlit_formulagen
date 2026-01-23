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
        'description': 'Number of years of premiums paid (FUP_Date - TERM_START_DATE) / 12',
        # FIX: Wrapped in int() to return a whole number and ignore decimals
        'formula': 'int(MONTHS_BETWEEN(TERM_START_DATE, FUP_Date) / 12)', 
        'variables': ['FUP_Date', 'TERM_START_DATE']
    },
    'policy_year': {
        'description': 'Policy year based on term start and surrender date',
        'formula': 'int(MONTHS_BETWEEN(TERM_START_DATE, DATE_OF_SURRENDER) / 12 + 1)', # Also applied int() here for consistency
        'variables': ['DATE_OF_SURRENDER', 'TERM_START_DATE']
    },
    'maturity_date': {
        'description': 'Maturity date calculation',
        'formula': 'ADD_MONTHS(TERM_START_DATE, BENEFIT_TERM * 12)',
        'variables': ['TERM_START_DATE', 'BENEFIT_TERM']
    },
    'Elapsed_policy_duration': {
        'description': 'Years elapsed since policy start',
        'formula': 'int(MONTHS_BETWEEN(TERM_START_DATE, CURRENT_DATE) / 12)', # Also applied int() here
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
            'is_pre_mapped': False
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
    """Calculate months between two dates (date2 - date1)"""
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
    """Safely evaluate a mathematical expression"""
    try:
        eval_expr = expression.strip()

        if '=' in eval_expr and not any(op in eval_expr for op in ['==', '!=', '<=', '>=']):
            parts = eval_expr.split('=')
            if len(parts) >= 2:
                eval_expr = parts[-1].strip()

        eval_expr = re.sub(
            r'(?<![a-zA-Z0-9_])(\d+(?:\.\d+)?)\s*%',
            r'(\1/100)',
            eval_expr
        )
        
        if 'MONTHS_BETWEEN' in eval_expr.upper():
            pattern = r'MONTHS_BETWEEN\s*\(\s*([^,]+)\s*,\s*([^)]+)\s*\)'
            matches = list(re.finditer(pattern, eval_expr, re.IGNORECASE))
            for match in reversed(matches):
                var1, var2 = match.group(1).strip(), match.group(2).strip()
                val1 = variables.get(var1, var1)
                val2 = variables.get(var2, var2)
                result = months_between(val1, val2)
                eval_expr = eval_expr[:match.start()] + str(result) + eval_expr[match.end():]
        
        if 'ADD_MONTHS' in eval_expr.upper():
            pattern = r'ADD_MONTHS\s*\(\s*([^,]+)\s*,\s*([^)]+)\s*\)'
            matches = list(re.finditer(pattern, eval_expr, re.IGNORECASE))
            for match in reversed(matches):
                var1, var2 = match.group(1).strip(), match.group(2).strip()
                val1 = variables.get(var1, var1)
                
                try:
                    var2_eval = var2
                    for var_name in sorted(variables.keys(), key=len, reverse=True):
                        if var_name in var2_eval:
                            var2_eval = var2_eval.replace(var_name, str(safe_convert_to_number(variables[var_name])))
                    val2_float = eval(var2_eval)
                except:
                    val2_float = safe_convert_to_number(var2)
                
                result = add_months(val1, val2_float)
                
                if result:
                    return result
                else:
                    eval_expr = eval_expr[:match.start()] + '0' + eval_expr[match.end():]
        
        if 'CURRENT_DATE' in eval_expr.upper():
            current_date = datetime.now()
            eval_expr = re.sub(r'\bCURRENT_DATE\b', f"'{current_date.strftime('%Y-%m-%d')}'", 
                              eval_expr, flags=re.IGNORECASE)
        
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
        
        sorted_vars = sorted(variables.keys(), key=len, reverse=True)
        
        for var_name in sorted_vars:
            value = variables[var_name]
            numeric_value = safe_convert_to_number(value)
            
            if var_name.startswith('[') and var_name.endswith(']'):
                eval_expr = eval_expr.replace(var_name, str(numeric_value))
            else:
                pattern = r'\b' + re.escape(var_name) + r'\b'
                eval_expr = re.sub(pattern, str(numeric_value), eval_expr, flags=re.IGNORECASE)
        
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
    HYBRID LOGIC: Handles [Bracketed Headers], Existing Columns, and Standard Variables.
    """

    var_values = {}
    
    # Create reverse mapping: variable_name -> header_name
    var_to_header_mapping = {v: k for k, v in header_to_var_mapping.items() if v}
    
    # 1. EXTRACT Bracketed Headers [Name] for Pre-Mapped formulas
    bracketed_headers = set()
    if is_pre_mapped:
        pattern = r'\[([^\]]+)\]'
        matches = re.findall(pattern, formula_expr)
        bracketed_headers.update(matches)
        
        for header_name in bracketed_headers:
            val = None
            
            # FIX: First check if this header_name is actually a variable that maps to a column
            if header_name in var_to_header_mapping:
                # This is a variable name, get the actual header it maps to
                actual_header = var_to_header_mapping[header_name]
                if actual_header in row.index:
                    val = row[actual_header]
            
            # If not found via mapping, try direct column lookup
            if val is None:
                if header_name in row.index:
                    val = row[header_name]
                else:
                    # Case-insensitive match
                    for col in row.index:
                        if col.lower() == header_name.lower():
                            val = row[col]
                            break
            
            if val is not None:
                var_values[f"[{header_name}]"] = val
            else:
                var_values[f"[{header_name}]"] = 0.0

    # 2. IDENTIFY Potential Variables
    potential_vars = set()
    
    # A) FIX: Extract variables inside MONTHS_BETWEEN first
    if 'MONTHS_BETWEEN' in formula_expr.upper():
        pattern = r'MONTHS_BETWEEN\s*\(\s*([^,]+)\s*,\s*([^)]+)\s*\)'
        matches = re.findall(pattern, formula_expr, flags=re.IGNORECASE)
        for match in matches:
            # Extract words from arguments (e.g., "VAR1", "VAR2" from "VAR1, VAR2")
            v1 = re.findall(r'\b\w+\b', match[0])
            v2 = re.findall(r'\b\w+\b', match[1])
            potential_vars.update(v1)
            potential_vars.update(v2)
            
    # B) FIX: Extract variables inside ADD_MONTHS
    if 'ADD_MONTHS' in formula_expr.upper():
        pattern = r'ADD_MONTHS\s*\(\s*([^,]+)\s*,\s*([^)]+)\s*\)'
        matches = re.findall(pattern, formula_expr, flags=re.IGNORECASE)
        for match in matches:
            v1 = re.findall(r'\b\w+\b', match[0])
            v2 = re.findall(r'\b\w+\b', match[1])
            potential_vars.update(v1)
            potential_vars.update(v2)

    # C) Find remaining variables in the cleaned expression
    # We remove functions first to avoid matching function names or internal operators
    clean_expr = re.sub(r'MONTHS_BETWEEN\([^)]+\)', '', formula_expr, flags=re.IGNORECASE)
    clean_expr = re.sub(r'ADD_MONTHS\([^)]+\)', '', clean_expr, flags=re.IGNORECASE)
    clean_expr = re.sub(r'\[[^\]]+\]', '', clean_expr)
    
    other_vars = set(re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', clean_expr))
    potential_vars.update(other_vars)
    
    # Filter out Python keywords and specific function names
    python_keywords = {'max', 'min', 'abs', 'round', 'sum', 'pow', 'math', 'sqrt', 'len', 'int', 'float', 'CURRENT_DATE'}
    potential_vars = potential_vars - python_keywords
    
    # 3. POPULATE var_values with found variables
    var_to_header_mapping = {v: k for k, v in header_to_var_mapping.items() if v}
    
    for var_name in potential_vars:
        # A) Check if it's an existing column (e.g. 'no_of_premium_paid' calculated earlier)
        if var_name in row.index:
            var_values[var_name] = row[var_name]
            continue
        
        # B) Check if it maps via header_to_var_mapping
        mapped_header = None
        
        if var_name in header_to_var_mapping and header_to_var_mapping[var_name]:
            mapped_header = header_to_var_mapping[var_name]
        elif var_name in var_to_header_mapping:
            mapped_header = var_to_header_mapping[var_name]
        
        if mapped_header:
            if mapped_header in row.index:
                var_values[var_name] = row[mapped_header]
            else:
                # Case-insensitive match
                for col in row.index:
                    if col.lower() == str(mapped_header).lower():
                        var_values[var_name] = row[col]
                        break
    
    result = safe_eval(formula_expr, var_values)
    return result

def find_matching_column(formula_name: str, df_columns: List[str], header_to_var_mapping: Dict[str, str]) -> str:
    """Find the best matching column for a formula"""
    formula_lower = formula_name.lower().replace('_', '').replace(' ', '')
    
    for col in df_columns:
        col_clean = col.lower().replace('_', '').replace(' ', '')
        if col_clean == formula_lower:
            return col
    
    for col in df_columns:
        col_lower = col.lower()
        fname_lower = formula_name.lower()
        if fname_lower in col_lower or col_lower in fname_lower:
            return col
    
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
    
    all_formulas = []
    if include_derived:
        derived = get_derived_formulas()
        for f in derived: 
            f['is_pre_mapped'] = False
        all_formulas.extend(derived)
        st.info(f"📊 Added {len(derived)} derived formulas (will run FIRST)")
    
    all_formulas.extend(formulas.copy())
    
    st.info(f"🔧 Processing {len(all_formulas)} total formulas in order")
    
    df_columns = df.columns.tolist()
    
    for formula in all_formulas:
        formula_name = formula.get('formula_name', 'Unknown')
        formula_expr = formula.get('formula_expression', '')
        is_pre_mapped = formula.get('is_pre_mapped', False)
        
        # FIX: Determine output column based on mapping
        output_col = formula_name  # Default to formula name
        
        # Check if this formula name has a mapping to a different column
        var_to_header_mapping = {v: k for k, v in header_to_var_mapping.items() if v}
        if formula_name in var_to_header_mapping:
            # Use the mapped column name instead
            output_col = var_to_header_mapping[formula_name]
            st.info(f"📍 **{formula_name}** mapped to column: **{output_col}**")
        else:
            # Otherwise use existing matching logic
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
        
        if total_rows > 0:
            first_row = result_df.iloc[0]
            
            with st.expander(f"🔍 Debug: {formula_name}"):
                if is_pre_mapped:
                    st.write("**Mode:** Pre-Mapped (Direct Header Lookup)")
                    pattern = r'\[([^\]]+)\]'
                    headers_in_formula = re.findall(pattern, formula_expr)
                    st.write(f"**Headers in brackets:** {headers_in_formula}")
                    
                    for h in headers_in_formula:
                        val = first_row.get(h, "Not Found")
                        st.write(f"  - `[{h}]` = {val}")
                else:
                    st.write("**Mode:** Variable Mapping / Hybrid")
                    st.write(f"**Variables used:** {formula.get('variables_used', 'Unknown')}")
                
                first_result = calculate_row(first_row, formula_expr, header_to_var_mapping, is_pre_mapped=is_pre_mapped)
                st.write(f"**Test Result:** {first_result}")
                
                if first_result is None:
                    st.error("⚠️ Test calculation returned None - check formula, headers, and variables")
        
        progress_bar = st.progress(0)
        status_text = st.empty()
        
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

# --- NEW: Test Formula Functionality ---
def extract_variables_from_formulas(formulas: List[Dict]) -> Dict[str, List[str]]:
    """Extract all unique variables from formulas JSON"""
    variables = {
        'bracketed': set(),
        'plain': set(),
        'derived': set()
    }
    
    for formula in formulas:
        formula_expr = formula.get('mapped_expression', formula.get('formula_expression', ''))
        
        bracketed_matches = re.findall(r'\[([^\]]+)\]', formula_expr)
        variables['bracketed'].update(bracketed_matches)
        
        clean_expr = re.sub(r'\[[^\]]+\]', '', formula_expr)
        clean_expr = re.sub(r'MONTHS_BETWEEN\([^)]+\)', '', clean_expr, flags=re.IGNORECASE)
        clean_expr = re.sub(r'ADD_MONTHS\([^)]+\)', '', clean_expr, flags=re.IGNORECASE)
        
        potential_vars = re.findall(r'\b[A-Z][A-Z0-9_]*\b', clean_expr)
        
        exclude_words = {'MAX', 'MIN', 'ABS', 'ROUND', 'SUM', 'POWER', 'SQRT', 'CURRENT_DATE', 'MONTHS_BETWEEN', 'ADD_MONTHS'}
        plain_vars = [var for var in potential_vars if var not in exclude_words and len(var) > 1]
        variables['plain'].update(plain_vars)
    
    for formula_name, info in BASIC_DERIVED_FORMULAS.items():
        variables['derived'].update(info['variables'])
    
    return {
        'bracketed': sorted(list(variables['bracketed'])) if variables['bracketed'] else [],
        'plain': sorted(list(variables['plain'])) if variables['plain'] else [],
        'derived': sorted(list(variables['derived'])) if variables['derived'] else []
    }

def get_smart_default_value(var_name: str):
    """Get smart default value based on variable name patterns"""
    var_upper = var_name.upper()
    
    # Date variables
    if 'DATE' in var_upper:
        if 'START' in var_upper:
            return date(2020, 1, 1)
        else:
            return date(2023, 6, 15)
    
    # Factor variables (0-1 range)
    if 'FACTOR' in var_upper:
        if 'GSV' in var_upper or 'SSV' in var_upper:
            return 0.3
        elif 'SV' in var_upper:
            return 0.5
        else:
            return 0.1
    
    # Premium/Amount variables
    if any(term in var_upper for term in ['PREMIUM', 'AMOUNT', 'BENEFIT', 'SUM']):
        if 'TERM' in var_upper:
            return 10.0  # FIX: Return float 10.0 instead of int 10
        elif 'FULL' in var_upper or 'TOTAL' in var_upper:
            return 50000.0
        elif 'INCOME' in var_upper:
            return 10000.0
        else:
            return 100000.0
    
    # Term variables
    if 'TERM' in var_upper:
        return 10.0  # FIX: Return float 10.0 instead of int 10
    
    # ROP (Return of Premium) variables
    if 'ROP' in var_upper:
        return 50000.0
    
    # Default numeric value
    return 1000.0
    
def test_formulas_interface():
    """Create an interface to test formulas with manual inputs"""
    st.markdown("### 🧪 Formula Testing Interface")
    st.markdown("Test your formulas and derived calculations before running on full dataset")
    
    formula_vars = {'bracketed': [], 'plain': [], 'derived': []}
    if 'formulas' in st.session_state and st.session_state.formulas:
        formula_vars = extract_variables_from_formulas(st.session_state.formulas)
    
    with st.expander("🔧 Test Single Formula", expanded=True):
        col1, col2 = st.columns([1, 2])
        
        with col1:
            st.markdown("#### Input Values")
            
            input_values = {}
            
            if formula_vars['derived']:
                st.markdown("**Derived Formula Variables:**")
                for var in formula_vars['derived']:
                    default_value = get_smart_default_value(var)
                    if 'DATE' in var.upper():
                        input_values[var] = st.date_input(var, value=default_value)
                    elif 'TERM' in var.upper() and var != 'FUP_Date':
                        input_values[var] = st.number_input(f"{var}", value=default_value, min_value=1.0)
                    else:
                        input_values[var] = st.date_input(var, value=default_value)
            
            if formula_vars['bracketed']:
                st.markdown("**Formula Variables (from mapped expressions):**")
                for var in formula_vars['bracketed']:
                    clean_var = var.replace('_', ' ').title()
                    default_value = get_smart_default_value(var)
                    
                    if 'DATE' in var.upper():
                        input_values[var] = st.date_input(f"{clean_var}", value=default_value)
                    elif 'FACTOR' in var.upper():
                        # FIX: Removed max_value=1.0. Factors can be greater than 1.
                        input_values[var] = st.number_input(f"{clean_var}", value=default_value, min_value=0.0, step=0.01)
                    elif any(term in var.upper() for term in ['PREMIUM', 'AMOUNT', 'BENEFIT', 'SUM', 'ROP']):
                        input_values[var] = st.number_input(f"{clean_var}", value=default_value, min_value=0.0)
                    else:
                        if isinstance(default_value, date):
                            default_value = default_value.year
                        input_values[var] = st.number_input(f"{var}", value=default_value, min_value=0.0)
            
            if formula_vars['plain']:
                st.markdown("**Additional Formula Variables:**")
                for var in formula_vars['plain']:
                    if var not in input_values:
                        clean_var = var.replace('_', ' ').title()
                        default_value = get_smart_default_value(var)
                        
                        if 'DATE' in var.upper():
                            input_values[var] = st.date_input(f"{clean_var}", value=default_value)
                        elif 'FACTOR' in var.upper():
                            # FIX: Removed max_value=1.0. Factors can be greater than 1.
                            input_values[var] = st.number_input(f"{clean_var}", value=default_value, min_value=0.0, step=0.01)
                        elif any(term in var.upper() for term in ['PREMIUM', 'AMOUNT', 'BENEFIT', 'SUM', 'ROP']):
                            input_values[var] = st.number_input(f"{clean_var}", value=default_value, min_value=0.0)
                        else:
                            input_values[var] = st.number_input(f"{var}", value=default_value, min_value=0.0)
            
        with col2:
            st.markdown("#### Test Results")
            
            test_row_data = {}
            
            for var in formula_vars['derived']:
                if var in input_values:
                    if 'DATE' in var.upper():
                        if isinstance(input_values[var], date):
                            test_row_data[var] = pd.to_datetime(input_values[var])
                        else:
                            test_row_data[var] = input_values[var]
                    else:
                        test_row_data[var] = input_values[var]
            
            for var in formula_vars['bracketed']:
                clean_var = var
                if var in input_values:
                    if 'DATE' in var.upper():
                        if isinstance(input_values[var], date):
                            test_row_data[clean_var] = pd.to_datetime(input_values[var])
                        else:
                            test_row_data[clean_var] = input_values[var]
                    else:
                        test_row_data[clean_var] = input_values[var]
            
            for var in formula_vars['plain']:
                if var not in test_row_data:
                    if var in input_values:
                        if 'DATE' in var.upper():
                            if isinstance(input_values[var], date):
                                test_row_data[var] = pd.to_datetime(input_values[var])
                            else:
                                test_row_data[var] = input_values[var]
                        else:
                            test_row_data[var] = input_values[var]
            
            test_row = pd.Series(test_row_data)
            working_row = test_row.copy()
            
            for var in formula_vars['derived']:
                if var in input_values:
                    if 'DATE' in var.upper():
                        working_row[var] = pd.to_datetime(input_values[var])
                    else:
                        working_row[var] = input_values[var]
            
            st.markdown("**Derived Formulas Results:**")
            derived_results = {}
            
            for formula_name, formula_info in BASIC_DERIVED_FORMULAS.items():
                result = calculate_row(working_row, formula_info['formula'], {}, is_pre_mapped=False)
                derived_results[formula_name] = result
                
                if result is not None:
                    working_row[formula_name] = result
                
                col_success, col_result = st.columns([3, 1])
                with col_success:
                    if result is not None and result != 0:
                        if isinstance(result, float) and result.is_integer():
                            st.success(f"✅ {formula_name}: {int(result)}")
                        else:
                            st.success(f"✅ {formula_name}: {result:.6f}")
                    else:
                        if result == 0:
                            st.warning(f"⚠️ {formula_name}: {result} (This will cause division issues in dependent formulas)")
                        else:
                            st.warning(f"⚠️ {formula_name}: {result} (Check inputs)")
                with col_result:
                    if st.button("Debug", key=f"debug_derived_{formula_name}"):
                        debug_data = {
                            'formula': formula_info['formula'],
                            'inputs': {var: str(working_row.get(var, 'Not found')) for var in formula_info['variables']},
                            'calculated_months': None,
                            'explanation': f"MONTHS_BETWEEN({working_row.get('TERM_START_DATE', 'missing')}, {working_row.get('FUP_Date', 'missing')}) / 12"
                        }
                        if 'FUP_Date' in working_row and 'TERM_START_DATE' in working_row:
                            debug_data['calculated_months'] = months_between(working_row['TERM_START_DATE'], working_row['FUP_Date'])
                            debug_data['final_result'] = f"{debug_data['calculated_months']} months ÷ 12 = {debug_data['calculated_months'] / 12:.2f} years"
                        st.json(debug_data)
            
            for key, value in derived_results.items():
                if value is not None:
                    working_row[key] = value
            
            st.markdown("---")
            st.markdown("**Your Formulas Results:**")
            
            if 'formulas' in st.session_state and st.session_state.formulas:
                formulas_to_process = st.session_state.formulas.copy()
                processed_formulas = {}
                max_iterations = len(formulas_to_process) * 2
                iterations = 0
                
                while formulas_to_process and iterations < max_iterations:
                    iterations += 1
                    formula = formulas_to_process.pop(0)
                    formula_name = formula.get('formula_name', 'Unknown')
                    formula_expr = formula.get('mapped_expression', formula.get('formula_expression', ''))
                    
                    if formula_expr:
                        try:
                            result = calculate_row(working_row, formula_expr, {}, is_pre_mapped=True)
                            
                            if result is not None:
                                working_row[formula_name] = result
                                processed_formulas[formula_name] = result
                                
                                col_success, col_result = st.columns([3, 1])
                                with col_success:
                                    if isinstance(result, (int, float)) and not (isinstance(result, float) and (math.isnan(result) or math.isinf(result))):
                                        st.success(f"✅ {formula_name}: {result:,.6f}")
                                    else:
                                        st.success(f"✅ {formula_name}: {result}")
                                with col_result:
                                    if st.button("Debug", key=f"debug_formula_{formula_name}"):
                                        relevant_inputs = {}
                                        for col in working_row.index:
                                            if f'[{col}]' in formula_expr or col in formula_expr:
                                                relevant_inputs[col] = working_row[col]
                                        
                                        st.json({
                                            'formula': formula_expr,
                                            'inputs': relevant_inputs,
                                            'result': result,
                                            'available_variables': list(working_row.index)
                                        })
                            else:
                                if iterations < max_iterations - 1:
                                    formulas_to_process.append(formula)
                                else:
                                    st.error(f"❌ {formula_name}: Failed to calculate after all dependencies")
                                    
                        except Exception as e:
                            st.error(f"❌ {formula_name}: {str(e)}")
                
                if formulas_to_process:
                    st.warning(f"⚠️ Could not process {len(formulas_to_process)} formulas due to dependency issues")
                    for formula in formulas_to_process:
                        st.caption(f"  - {formula.get('formula_name', 'Unknown')}")
                        
            else:
                st.info("No formulas loaded. Upload formulas JSON to test them.")

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
            st.info("📊 Extraction format detected")
        elif isinstance(data, list):
            formulas = data
        else:
            raise ValueError("Invalid format")
        
        validated_formulas = []
        for formula in formulas:
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
    
    test_formulas_interface()
    
    st.markdown("---")
    
    has_mappings = 'header_to_var_mapping' in st.session_state and st.session_state.header_to_var_mapping
    has_formulas = 'formulas' in st.session_state and st.session_state.formulas
    has_data = 'excel_df' in st.session_state and st.session_state.excel_df is not None
    
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
    
    include_derived = st.checkbox("Include derived formulas", value=True, 
                                  help="Automatically include standard derived calculations")
    
    if include_derived:
        with st.expander("📋 Derived Formulas"):
            for name, info in BASIC_DERIVED_FORMULAS.items():
                st.write(f"**{name}**: {info['description']}")
                st.code(info['formula'])
    
    st.markdown("---")
    
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
        
        from io import BytesIO
        col1, col2 = st.columns(2)
        
        with col1:
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