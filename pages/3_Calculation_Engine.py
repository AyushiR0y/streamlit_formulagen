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
def format_indian_number(num):
    """Format number in Indian numbering system (lakhs, crores)"""
    if pd.isna(num) or num is None:
        return "N/A"
    
    num = float(num)
    
    if num < 0:
        sign = "-"
        num = abs(num)
    else:
        sign = ""
    
    s = f"{num:.2f}"
    if '.' in s:
        integer_part, decimal_part = s.split('.')
    else:
        integer_part = s
        decimal_part = "00"
    
    # Indian formatting: last 3 digits, then groups of 2
    if len(integer_part) <= 3:
        formatted = integer_part
    else:
        last_three = integer_part[-3:]
        remaining = integer_part[:-3]
        
        # Group remaining digits in pairs from right to left
        pairs = []
        while len(remaining) > 0:
            if len(remaining) >= 2:
                pairs.insert(0, remaining[-2:])
                remaining = remaining[:-2]
            else:
                pairs.insert(0, remaining)
                remaining = ""
        
        formatted = ",".join(pairs) + "," + last_three
    
    return f"{sign}{formatted}.{decimal_part}"
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
    }
}

FORMULA_ALIASES = {
    # Multiple formula names that should write to/read from the same column
    'ROP_BENEFIT': 'TOTAL_PREMIUM_PAID',
    'ROP_Benefit': 'TOTAL_PREMIUM_PAID',  # Handle case variations
    'TOTAL_PREMIUMS_PAID': 'TOTAL_PREMIUM_PAID',
    'Income_Benefit_Amount': 'PAID_UP_INCOME_BENEFIT_AMOUNT',
    
    
    # Common formula name variations
    'PAID_UP_SA_ON_DEATH': 'PAID_UP_SA_ON_DEATH',  # Should use calculated column, not map back
    'Present_Value_of_paid_up_sum_assured_on_death': 'PAID_UP_SA_ON_DEATH',
    'PAID_UP_INCOME_INSTALLMENT': 'PAID_UP_INCOME_BENEFIT_AMOUNT',  

}


def get_output_column_name(formula_name: str, var_to_header_mapping: Dict[str, str]) -> str:
    """
    Determine the actual column name to use for a formula.
    Checks: aliases → mappings → formula name itself
    """
    # Check if this formula has an alias
    if formula_name in FORMULA_ALIASES:
        return FORMULA_ALIASES[formula_name]
    
    # Check if formula maps to a specific header
    if formula_name in var_to_header_mapping:
        return var_to_header_mapping[formula_name]
    
    # Default to formula name itself
    return formula_name
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
    """Safely evaluate - Version 2 with better MAX handling"""
    try:
        eval_expr = expression.strip()

        if '=' in eval_expr and not any(op in eval_expr for op in ['==', '!=', '<=', '>=']):
            parts = eval_expr.split('=')
            if len(parts) >= 2:
                eval_expr = parts[-1].strip()

        # Process date functions
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
        
        # Map functions - case insensitive
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
        
        # Replace variables
        sorted_vars = sorted(variables.keys(), key=len, reverse=True)
        token_map = {}
        
        for idx, var_name in enumerate(sorted_vars):
            value = variables[var_name]
            numeric_value = safe_convert_to_number(value)
            token = f"___VAR_{idx}___"
            token_map[token] = numeric_value
            
            if var_name.startswith('[') and var_name.endswith(']'):
                eval_expr = eval_expr.replace(var_name, token)
            else:
                pattern = r'\b' + re.escape(var_name) + r'\b'
                eval_expr = re.sub(pattern, token, eval_expr, flags=re.IGNORECASE)
        
        # Replace tokens with values
        for token, numeric_value in token_map.items():
            # Use repr() to ensure proper formatting
            eval_expr = eval_expr.replace(token, repr(numeric_value))
        
        # Convert percentages - ensure proper float division
        eval_expr = re.sub(r'(\d+(?:\.\d+)?)\s*%', r'((\1)/100.0)', eval_expr)
        
        print(f"🔧 Final expression: {eval_expr}")
        
        allowed_builtins = {
            'max': max, 'min': min, 'abs': abs, 'round': round,
            'int': int, 'float': float, 'pow': pow, 'sum': sum, 'len': len
        }
        
        result = eval(eval_expr, {"__builtins__": allowed_builtins, "math": math}, {})
        
        print(f"✅ Result: {result} (type: {type(result).__name__})")
        
        if isinstance(result, (int, float)):
            if math.isnan(result) or math.isinf(result):
                return None
            return float(result)
        elif isinstance(result, (datetime, date, pd.Timestamp)):
            return result
        else:
            return None
    
    except Exception as e:
        print(f"❌ Error in safe_eval_v2: {e}")
        print(f"   Expression: {expression}")
        print(f"   After processing: {eval_expr if 'eval_expr' in locals() else 'N/A'}")
        import traceback
        traceback.print_exc()
        return None

def calculate_row(row: pd.Series, formula_expr: str, header_to_var_mapping: Dict[str, str], is_pre_mapped: bool = False) -> Any:
    """
    Calculate formula result for a single row.
    HYBRID LOGIC: Handles [Bracketed Headers], Existing Columns, and Standard Variables.
    PRIORITY: Calculated columns > Aliases > Direct headers > Mappings
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
            
            # PRIORITY 1: Check if this is a CALCULATED COLUMN (highest priority)
            if header_name in row.index:
                val = row[header_name]
                if pd.notna(val):
                    var_values[f"[{header_name}]"] = val
                    continue
            
            # PRIORITY 2: Check if this is an aliased variable
            if header_name in FORMULA_ALIASES:
                actual_column_to_check = FORMULA_ALIASES[header_name]
                if actual_column_to_check in row.index:
                    val = row[actual_column_to_check]
                    if pd.notna(val):
                        var_values[f"[{header_name}]"] = val
                        continue
            
            # PRIORITY 3: Check if header_name is a variable that maps to a column
            if header_name in var_to_header_mapping:
                actual_header = var_to_header_mapping[header_name]
                if actual_header in row.index:
                    val = row[actual_header]
                    if pd.notna(val):
                        var_values[f"[{header_name}]"] = val
                        continue
            
            # PRIORITY 4: Case-insensitive column match
            for col in row.index:
                if col.lower() == header_name.lower():
                    val = row[col]
                    if pd.notna(val):
                        var_values[f"[{header_name}]"] = val
                        break
            
            if f"[{header_name}]" not in var_values:
                var_values[f"[{header_name}]"] = 0.0

    # 2. IDENTIFY Potential Variables (non-bracketed)
    potential_vars = set()
    
    # Extract from MONTHS_BETWEEN
    if 'MONTHS_BETWEEN' in formula_expr.upper():
        pattern = r'MONTHS_BETWEEN\s*\(\s*([^,]+)\s*,\s*([^)]+)\s*\)'
        matches = re.findall(pattern, formula_expr, flags=re.IGNORECASE)
        for match in matches:
            v1 = re.findall(r'\b\w+\b', match[0])
            v2 = re.findall(r'\b\w+\b', match[1])
            potential_vars.update(v1)
            potential_vars.update(v2)
            
    # Extract from ADD_MONTHS
    if 'ADD_MONTHS' in formula_expr.upper():
        pattern = r'ADD_MONTHS\s*\(\s*([^,]+)\s*,\s*([^)]+)\s*\)'
        matches = re.findall(pattern, formula_expr, flags=re.IGNORECASE)
        for match in matches:
            v1 = re.findall(r'\b\w+\b', match[0])
            v2 = re.findall(r'\b\w+\b', match[1])
            potential_vars.update(v1)
            potential_vars.update(v2)

    # Extract other variables (but exclude function calls and bracketed ones)
    clean_expr = re.sub(r'MONTHS_BETWEEN\([^)]+\)', '', formula_expr, flags=re.IGNORECASE)
    clean_expr = re.sub(r'ADD_MONTHS\([^)]+\)', '', clean_expr, flags=re.IGNORECASE)
    clean_expr = re.sub(r'\[[^\]]+\]', '', clean_expr)
    
    # FIX: Remove function calls like MAX(...), MIN(...) before extracting variables
    # But we need to keep the contents of MAX/MIN to extract variables from inside them
    temp_expr = clean_expr
    # Remove the function name but keep contents
    temp_expr = re.sub(r'\b(MAX|MIN|ABS|ROUND|SUM|POWER|SQRT|POW)\s*\(', '(', temp_expr, flags=re.IGNORECASE)
    
    other_vars = set(re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', temp_expr))
    potential_vars.update(other_vars)
    
    # UPDATED: More comprehensive keyword list
    python_keywords = {
        'max', 'min', 'abs', 'round', 'sum', 'pow', 'math', 'sqrt', 'len', 'int', 'float', 
        'CURRENT_DATE', 'MAX', 'MIN', 'ABS', 'ROUND', 'SUM', 'POWER', 'SQRT', 'POW'
    }
    potential_vars = potential_vars - python_keywords
    
    # 3. POPULATE var_values with found variables
    # PRIORITY ORDER: Calculated columns > Aliases > Direct headers > Mappings
    for var_name in potential_vars:
        # Skip if already added as bracketed variable
        if f"[{var_name}]" in var_values:
            continue
            
        val = None
        found_source = None
        
        # PRIORITY 1: Check if it's a calculated column (direct match) - HIGHEST PRIORITY
        if var_name in row.index:
            val = row[var_name]
            if pd.notna(val):
                var_values[var_name] = val
                found_source = f"Calculated Column: {var_name}"
                print(f"✓ Found {var_name} as calculated column = {val}")
                continue
            else:
                print(f"⚠ Found {var_name} in row.index but value is NaN")
        
        # PRIORITY 2: Check if it's an aliased formula (e.g., ROP_BENEFIT → TOTAL_PREMIUM_PAID)
        if var_name in FORMULA_ALIASES:
            aliased_col = FORMULA_ALIASES[var_name]
            if aliased_col in row.index:
                val = row[aliased_col]
                if pd.notna(val):
                    var_values[var_name] = val
                    found_source = f"Alias: {var_name} → {aliased_col}"
                    print(f"✓ Found {var_name} via alias {aliased_col} = {val}")
                    continue
        
        # PRIORITY 3: Check if var maps to a header via header_to_var_mapping
        mapped_header = None
        
        if var_name in header_to_var_mapping and header_to_var_mapping[var_name]:
            mapped_header = header_to_var_mapping[var_name]
        elif var_name in var_to_header_mapping:
            mapped_header = var_to_header_mapping[var_name]
        
        if mapped_header and mapped_header in row.index:
            val = row[mapped_header]
            if pd.notna(val):
                var_values[var_name] = val
                found_source = f"Mapping: {var_name} → {mapped_header}"
                print(f"✓ Found {var_name} via mapping {mapped_header} = {val}")
                continue
        
        # PRIORITY 4: Case-insensitive search
        if val is None:
            for col in row.index:
                if col.lower() == var_name.lower():
                    val = row[col]
                    if pd.notna(val):
                        var_values[var_name] = val
                        found_source = f"Case-insensitive: {var_name} → {col}"
                        print(f"✓ Found {var_name} case-insensitive as {col} = {val}")
                        break
        
        # If still not found, default to 0.0
        if var_name not in var_values:
            print(f"❌ WARNING: Variable '{var_name}' not found in row, defaulting to 0.0")
            print(f"   Available columns: {list(row.index)[:10]}...")  # Show first 10 columns
            var_values[var_name] = 0.0
    
    # DEBUG: Print what variables we're passing to safe_eval
    print(f"\n▶ Calculating: {formula_expr}")
    print(f"▶ Variables being passed to safe_eval: {list(var_values.keys())}")
    
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
    
    # Create reverse mapping: variable_name -> header_name
    var_to_header_mapping = {v: k for k, v in header_to_var_mapping.items() if v}
    
    all_formulas = []
    if include_derived:
        derived = get_derived_formulas()
        for f in derived: 
            f['is_pre_mapped'] = False
        all_formulas.extend(derived)
        st.info(f"📊 Added {len(derived)} derived formulas (will run FIRST)")
    
    all_formulas.extend(formulas.copy())
    
    st.info(f"🔧 Processing {len(all_formulas)} total formulas in order")
    
    for formula in all_formulas:
        formula_name = formula.get('formula_name', 'Unknown')
        formula_expr = formula.get('formula_expression', '')
        is_pre_mapped = formula.get('is_pre_mapped', False)
        
        # UPDATED: Determine output column using alias/mapping logic
        output_col = get_output_column_name(formula_name, var_to_header_mapping)
        
        # Show if we're using an alias or mapping
        if output_col != formula_name:
            if formula_name in FORMULA_ALIASES:
                st.info(f"🔗 **{formula_name}** → aliased to column: **{output_col}**")
            else:
                st.info(f"📍 **{formula_name}** → mapped to column: **{output_col}**")
        
        # Create output column if it doesn't exist
        col_existed = output_col in result_df.columns
        
        if not col_existed:
            result_df[output_col] = np.nan
            st.info(f"📝 Creating new column: **{output_col}**")
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
                    pattern = r'\[([^\]]+)\]'
                    headers_in_formula = re.findall(pattern, formula_expr)
                    st.write(f"**Headers in brackets:** {headers_in_formula}")
                    
                    for h in headers_in_formula:
                        # Check via mapping
                        val = None
                        if h in var_to_header_mapping:
                            actual_col = var_to_header_mapping[h]
                            val = first_row.get(actual_col, "Not found")
                            st.write(f"  - `[{h}]` → `{actual_col}` = {val}")
                        elif h in first_row.index:
                            val = first_row[h]
                            st.write(f"  - `[{h}]` (direct) = {val}")
                        else:
                            st.write(f"  - `[{h}]` = Not Found")
                else:
                    st.write("**Mode:** Variable Mapping / Hybrid")
                    st.write(f"**Variables used:** {formula.get('variables_used', 'Unknown')}")
                
                first_result = calculate_row(first_row, formula_expr, header_to_var_mapping, is_pre_mapped=is_pre_mapped)
                st.write(f"**Test Result:** {first_result}")
                st.write(f"**Will write to column:** {output_col}")
                
                if first_result is None:
                    st.error("⚠️ Test calculation returned None - check formula, headers, and variables")
        
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        # ROW-BY-ROW CALCULATION
        for idx in range(len(result_df)):
            try:
                row = result_df.iloc[idx]
                result = calculate_row(row, formula_expr, header_to_var_mapping, is_pre_mapped=is_pre_mapped)
                
                if result is None:
                    if idx < 5:
                        errors.append(f"Row {idx}: Calculation returned None")
                    result_df.at[result_df.index[idx], output_col] = np.nan
                else:
                    # Write to the determined output column
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
            st.write(f"Sample values in **{output_col}**: {sample_vals}")
        
        calculation_results.append(CalculationResult(
            formula_name=f"{formula_name} → {output_col}",
            rows_calculated=success_count,
            errors=errors[:10],
            success_rate=success_rate
        ))
        
    
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
def show_detailed_calculations(result_df: pd.DataFrame, formulas: List[Dict], 
                               header_to_var_mapping: Dict[str, str], 
                               num_rows: int = 3):
    """Show detailed step-by-step calculations for the first N rows"""
    
    st.markdown("---")
    st.markdown(f"### 🔍 Detailed Calculation Breakdown (First {num_rows} Rows)")
    
    var_to_header_mapping = {v: k for k, v in header_to_var_mapping.items() if v}
    
    # Include derived formulas
    all_formulas = get_derived_formulas() + formulas
    
    for row_idx in range(min(num_rows, len(result_df))):
        st.markdown(f"## 📊 Row {row_idx + 1}")
        
        row = result_df.iloc[row_idx]
        
        # Show original data values
        with st.expander(f"📋 Original Data - Row {row_idx + 1}", expanded=False):
            original_data = {}
            for col in result_df.columns:
                if col not in [f['formula_name'] for f in all_formulas]:
                    original_data[col] = row[col]
            st.json(original_data)
        
        # Process each formula
        for formula_idx, formula in enumerate(all_formulas):
            formula_name = formula.get('formula_name', 'Unknown')
            formula_expr = formula.get('formula_expression', '')
            is_pre_mapped = formula.get('is_pre_mapped', False)
            
            output_col = get_output_column_name(formula_name, var_to_header_mapping)
            
            with st.expander(f"{'🔧' if formula_idx < len(get_derived_formulas()) else '📐'} {formula_name} → {output_col}", expanded=True):
                
                # Show formula
                st.code(formula_expr, language="python")
                
                # Extract variables used
                var_values = {}
                

                if is_pre_mapped:
                    # Bracketed variables
                    pattern = r'\[([^\]]+)\]'
                    matches = re.findall(pattern, formula_expr)
                    
                    st.markdown("**Variable Lookup:**")
                    lookup_table = []
                    
                    for header_name in matches:
                        val = None
                        source = "Not Found"
                        
                        # PRIORITY 1: Calculated column
                        if header_name in row.index:
                            val = row[header_name]
                            if pd.notna(val):
                                source = "Calculated Column"
                        
                        # PRIORITY 2: Check alias
                        if val is None or pd.isna(val):
                            if header_name in FORMULA_ALIASES:
                                aliased_col = FORMULA_ALIASES[header_name]
                                if aliased_col in row.index:
                                    val = row[aliased_col]
                                    if pd.notna(val):
                                        source = f"Alias → {aliased_col}"
                        
                        # PRIORITY 3: Check mapping
                        if val is None or pd.isna(val):
                            if header_name in var_to_header_mapping:
                                actual_header = var_to_header_mapping[header_name]
                                if actual_header in row.index:
                                    val = row[actual_header]
                                    if pd.notna(val):
                                        source = f"Mapping → {actual_header}"
                        
                        if val is None or pd.isna(val):
                            val = 0.0
                            source = "Default (0.0)"
                        
                        var_values[f"[{header_name}]"] = val
                        lookup_table.append({
                            'Variable': f'[{header_name}]',
                            'Value': val,
                            'Source': source
                        })
                    
                    # NEW: ALSO extract non-bracketed variables for pre-mapped formulas
                    clean_expr = re.sub(r'\[[^\]]+\]', '', formula_expr)
                    all_vars = set(re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', clean_expr))
                    python_keywords = {
                        'max', 'min', 'abs', 'round', 'sum', 'pow', 'math', 'sqrt', 'len', 'int', 'float', 
                        'CURRENT_DATE', 'MAX', 'MIN', 'ABS', 'ROUND', 'SUM', 'POWER', 'SQRT', 'POW',
                        'MONTHS_BETWEEN', 'ADD_MONTHS'
                    }
                    potential_vars = all_vars - python_keywords
                    
                    for var_name in sorted(potential_vars):
                        if f"[{var_name}]" in var_values:
                            continue
                            
                        val = None
                        source = "Not Found"
                        
                        # PRIORITY 1: Calculated column
                        if var_name in row.index:
                            val = row[var_name]
                            if pd.notna(val):
                                source = "Calculated Column"
                        
                        # PRIORITY 2: Check alias
                        if val is None or pd.isna(val):
                            if var_name in FORMULA_ALIASES:
                                aliased_col = FORMULA_ALIASES[var_name]
                                if aliased_col in row.index:
                                    val = row[aliased_col]
                                    if pd.notna(val):
                                        source = f"Alias → {aliased_col}"
                        
                        # PRIORITY 3: Check mapping
                        if val is None or pd.isna(val):
                            if var_name in header_to_var_mapping and header_to_var_mapping[var_name]:
                                mapped_header = header_to_var_mapping[var_name]
                                if mapped_header in row.index:
                                    val = row[mapped_header]
                                    if pd.notna(val):
                                        source = f"Mapping → {mapped_header}"
                            elif var_name in var_to_header_mapping:
                                mapped_header = var_to_header_mapping[var_name]
                                if mapped_header in row.index:
                                    val = row[mapped_header]
                                    if pd.notna(val):
                                        source = f"Reverse Mapping → {mapped_header}"
                        
                        if val is None or pd.isna(val):
                            val = 0.0
                            source = "Default (0.0)"
                        
                        var_values[var_name] = val
                        lookup_table.append({
                            'Variable': var_name,
                            'Value': val,
                            'Source': source
                        })
                    
                    if lookup_table:
                        st.table(pd.DataFrame(lookup_table))
                
                else:
                    # Non-bracketed variables
                    # Extract from MONTHS_BETWEEN
                    potential_vars = set()
                    if 'MONTHS_BETWEEN' in formula_expr.upper():
                        pattern = r'MONTHS_BETWEEN\s*\(\s*([^,]+)\s*,\s*([^)]+)\s*\)'
                        matches = re.findall(pattern, formula_expr, flags=re.IGNORECASE)
                        for match in matches:
                            v1 = re.findall(r'\b\w+\b', match[0])
                            v2 = re.findall(r'\b\w+\b', match[1])
                            potential_vars.update(v1)
                            potential_vars.update(v2)
                    
                    # Extract from ADD_MONTHS
                    if 'ADD_MONTHS' in formula_expr.upper():
                        pattern = r'ADD_MONTHS\s*\(\s*([^,]+)\s*,\s*([^)]+)\s*\)'
                        matches = re.findall(pattern, formula_expr, flags=re.IGNORECASE)
                        for match in matches:
                            v1 = re.findall(r'\b\w+\b', match[0])
                            v2 = re.findall(r'\b\w+\b', match[1])
                            potential_vars.update(v1)
                            potential_vars.update(v2)
                    
                    # Other variables
                    clean_expr = re.sub(r'MONTHS_BETWEEN\([^)]+\)', '', formula_expr, flags=re.IGNORECASE)
                    clean_expr = re.sub(r'ADD_MONTHS\([^)]+\)', '', clean_expr, flags=re.IGNORECASE)
                    other_vars = set(re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', clean_expr))
                    potential_vars.update(other_vars)
                    
                    python_keywords = {'max', 'min', 'abs', 'round', 'sum', 'pow', 'math', 'sqrt', 'len', 'int', 'float', 'CURRENT_DATE'}
                    potential_vars = potential_vars - python_keywords
                    
                    st.markdown("**Variable Lookup:**")
                    lookup_table = []
                    
                    for var_name in sorted(potential_vars):
                        val = None
                        source = "Not Found"
                        
                        # PRIORITY 1: Calculated column
                        if var_name in row.index:
                            val = row[var_name]
                            if pd.notna(val):
                                source = "Calculated Column"
                        
                        # PRIORITY 2: Check alias
                        if val is None or pd.isna(val):
                            if var_name in FORMULA_ALIASES:
                                aliased_col = FORMULA_ALIASES[var_name]
                                if aliased_col in row.index:
                                    val = row[aliased_col]
                                    if pd.notna(val):
                                        source = f"Alias → {aliased_col}"
                        
                        # PRIORITY 3: Check mapping
                        if val is None or pd.isna(val):
                            if var_name in header_to_var_mapping and header_to_var_mapping[var_name]:
                                mapped_header = header_to_var_mapping[var_name]
                                if mapped_header in row.index:
                                    val = row[mapped_header]
                                    if pd.notna(val):
                                        source = f"Mapping → {mapped_header}"
                            elif var_name in var_to_header_mapping:
                                mapped_header = var_to_header_mapping[var_name]
                                if mapped_header in row.index:
                                    val = row[mapped_header]
                                    if pd.notna(val):
                                        source = f"Reverse Mapping → {mapped_header}"
                        
                        if val is None or pd.isna(val):
                            val = 0.0
                            source = "Default (0.0)"
                        
                        var_values[var_name] = val
                        lookup_table.append({
                            'Variable': var_name,
                            'Value': val,
                            'Source': source
                        })
                    
                    if lookup_table:
                        st.table(pd.DataFrame(lookup_table))
                
                # Calculate result
                try:
                    result = calculate_row(row, formula_expr, header_to_var_mapping, is_pre_mapped=is_pre_mapped)
                    
                    col1, col2 = st.columns([1, 2])
                    with col1:
                        st.markdown("**Calculation:**")
                    with col2:
                        # Show simplified expression with values substituted
                        simplified_expr = formula_expr
                        for var, val in var_values.items():
                            if isinstance(val, (int, float)):
                                simplified_expr = simplified_expr.replace(var, f"{val:.6f}")
                            else:
                                simplified_expr = simplified_expr.replace(var, str(val))
                        st.code(simplified_expr, language="python")
                    
                    if result is not None:
                        if isinstance(result, (datetime, pd.Timestamp)):
                            st.success(f"✅ **Result:** {result.strftime('%Y-%m-%d')}")
                        elif isinstance(result, float):
                            st.success(f"✅ **Result:** {result:,.6f}")
                        else:
                            st.success(f"✅ **Result:** {result}")
                        
                        st.info(f"💾 Stored in column: **{output_col}**")
                    else:
                        st.error("❌ **Result:** None (calculation failed)")
                        
                except Exception as e:
                    st.error(f"❌ **Error:** {str(e)}")
                    import traceback
                    st.code(traceback.format_exc())
                
                st.markdown("---")
        
        st.markdown("---")
        st.markdown("---")
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
        
    include_derived = st.checkbox("Include derived formulas", value=True, 
                                  help="Automatically include standard derived calculations")
    
    if include_derived:
        with st.expander("📋 Derived Formulas"):
            for name, info in BASIC_DERIVED_FORMULAS.items():
                st.write(f"**{name}**: {info['description']}")
                st.code(info['formula'])
        
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
                
        with st.expander("📊 Formula Details", expanded=False):
            for calc_result in st.session_state.calc_results:
                icon = "✅" if calc_result.success_rate >= 90 else "⚠️" if calc_result.success_rate >= 50 else "❌"
                st.write(f"{icon} **{calc_result.formula_name}**: {calc_result.success_rate:.1f}%")
                if calc_result.errors:
                    for err in calc_result.errors[:3]:
                        st.caption(f"  {err}")
        
        st.dataframe(st.session_state.results_df, use_container_width=True, height=400)
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
            
            # NEW: Add detailed calculation view
            if st.checkbox("🔍 Show Detailed Calculations (First 3 Rows)", value=False):
                show_detailed_calculations(
                    st.session_state.results_df, 
                    st.session_state.formulas,
                    st.session_state.header_to_var_mapping,
                    num_rows=3
                )
            
            
            with st.expander("📊 Formula Details", expanded=False):
                for calc_result in st.session_state.calc_results:
                    icon = "✅" if calc_result.success_rate >= 90 else "⚠️" if calc_result.success_rate >= 50 else "❌"
                    st.write(f"{icon} **{calc_result.formula_name}**: {calc_result.success_rate:.1f}%")
                    if calc_result.errors:
                        for err in calc_result.errors[:3]:
                            st.caption(f"  {err}")
            
            # ... rest of the results display code
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