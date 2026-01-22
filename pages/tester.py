import streamlit as st
import pandas as pd
import numpy as np
import json
import re
import math
from datetime import datetime
from dateutil.relativedelta import relativedelta

st.set_page_config(page_title="Formula Tester", page_icon="🧪", layout="wide")

st.title("🧪 Formula Testing - Single Policy")
st.markdown("Test your formulas with manual input values to debug calculation issues")
st.markdown("---")

# Helper functions
def safe_convert_to_number(value):
    """Convert value to float"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0.0
    if isinstance(value, str) and value.strip() == '':
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(',', '').replace('$', '').strip())
    except:
        return 0.0

def months_between(date1, date2):
    """Calculate months between dates (date2 - date1)"""
    try:
        d1 = pd.to_datetime(date1)
        d2 = pd.to_datetime(date2)
        return float((d2.year - d1.year) * 12 + (d2.month - d1.month))
    except:
        return 0

def add_months(date, months):
    """Add months to date"""
    try:
        d = pd.to_datetime(date)
        return d + relativedelta(months=int(months))
    except:
        return None

def safe_eval(expression, variables):
    """Evaluate formula expression"""
    try:
        eval_expr = expression.strip()
        
        # Remove assignment if present (e.g., "var = expr" -> "expr")
        if '=' in eval_expr and not any(op in eval_expr for op in ['==', '!=', '<=', '>=']):
            parts = eval_expr.split('=')
            if len(parts) >= 2:
                eval_expr = parts[-1].strip()
        
        # Handle percentage literals (e.g., "105%" -> "105/100")
        eval_expr = re.sub(r'(\d+(?:\.\d+)?)\s*%', r'(\1/100)', eval_expr)
        
        # Handle MONTHS_BETWEEN
        if 'MONTHS_BETWEEN' in eval_expr.upper():
            pattern = r'MONTHS_BETWEEN\s*\(\s*([^,]+)\s*,\s*([^)]+)\s*\)'
            matches = list(re.finditer(pattern, eval_expr, re.IGNORECASE))
            for match in reversed(matches):
                var1, var2 = match.group(1).strip(), match.group(2).strip()
                val1 = variables.get(var1, var1)
                val2 = variables.get(var2, var2)
                result = months_between(val1, val2)
                eval_expr = eval_expr[:match.start()] + str(result) + eval_expr[match.end():]
        
        # Handle ADD_MONTHS
        if 'ADD_MONTHS' in eval_expr.upper():
            pattern = r'ADD_MONTHS\s*\(\s*([^,]+)\s*,\s*([^)]+)\s*\)'
            matches = list(re.finditer(pattern, eval_expr, re.IGNORECASE))
            for match in reversed(matches):
                var1, var2 = match.group(1).strip(), match.group(2).strip()
                val1 = variables.get(var1, var1)
                var2_eval = var2
                for var_name in sorted(variables.keys(), key=len, reverse=True):
                    if var_name in var2_eval:
                        var2_eval = var2_eval.replace(var_name, str(safe_convert_to_number(variables[var_name])))
                try:
                    val2_float = eval(var2_eval)
                except:
                    val2_float = safe_convert_to_number(var2)
                result = add_months(val1, val2_float)
                if result:
                    return result
                else:
                    eval_expr = eval_expr[:match.start()] + '0' + eval_expr[match.end():]
        
        # Handle CURRENT_DATE
        if 'CURRENT_DATE' in eval_expr.upper():
            current_date = datetime.now()
            eval_expr = re.sub(r'\bCURRENT_DATE\b', f"'{current_date.strftime('%Y-%m-%d')}'", 
                              eval_expr, flags=re.IGNORECASE)
        
        # Function mappings
        func_mappings = {
            r'\bMAX\s*\(': 'max(',
            r'\bMIN\s*\(': 'min(',
            r'\bABS\s*\(': 'abs(',
            r'\bROUND\s*\(': 'round(',
            r'\bPOWER\s*\(': 'pow(',
            r'\bSQRT\s*\(': 'math.sqrt(',
        }
        for pattern, replacement in func_mappings.items():
            eval_expr = re.sub(pattern, replacement, eval_expr, flags=re.IGNORECASE)
        
        # Replace variables
        sorted_vars = sorted(variables.keys(), key=len, reverse=True)
        for var_name in sorted_vars:
            value = variables[var_name]
            numeric_value = safe_convert_to_number(value)
            
            if var_name.startswith('[') and var_name.endswith(']'):
                eval_expr = eval_expr.replace(var_name, str(numeric_value))
            else:
                pattern = r'\b' + re.escape(var_name) + r'\b'
                eval_expr = re.sub(pattern, str(numeric_value), eval_expr, flags=re.IGNORECASE)
        
        # Evaluate
        allowed_builtins = {
            'max': max, 'min': min, 'abs': abs, 'round': round,
            'int': int, 'float': float, 'pow': pow
        }
        result = eval(eval_expr, {"__builtins__": allowed_builtins, "math": math}, {})
        
        if isinstance(result, (int, float)) and not (math.isnan(result) or math.isinf(result)):
            return float(result)
        return None
    except Exception as e:
        st.error(f"Evaluation error: {e}")
        st.code(f"Expression: {expression}\nAfter processing: {eval_expr}")
        return None

# Step 1: Upload formulas
st.header("Step 1: Upload Formulas JSON")
uploaded_formulas = st.file_uploader("Upload formulas JSON file", type=['json'])

if uploaded_formulas:
    formulas_data = json.loads(uploaded_formulas.read())
    st.success(f"✅ Loaded {len(formulas_data)} formulas")
    
    with st.expander("📋 View Formulas"):
        for idx, formula in enumerate(formulas_data, 1):
            st.write(f"**{idx}. {formula['formula_name']}**")
            st.code(formula['mapped_expression'])
    
    st.markdown("---")
    
    # Step 2: Input values
    st.header("Step 2: Input Values")
    st.info("Enter values for all variables needed by the formulas")
    
    # Collect all unique variables from formulas
    all_vars = set()
    
    # Extract from mapped expressions
    for formula in formulas_data:
        expr = formula.get('mapped_expression', '')
        
        # Extract bracketed variables [VAR_NAME]
        bracketed = re.findall(r'\[([^\]]+)\]', expr)
        for var in bracketed:
            all_vars.add(f"[{var}]")
        
        # Extract non-bracketed variables (word characters not in brackets)
        clean_expr = re.sub(r'\[[^\]]+\]', '', expr)
        words = re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', clean_expr)
        keywords = {'max', 'min', 'abs', 'round', 'pow', 'math', 'sqrt', 'MONTHS_BETWEEN', 'ADD_MONTHS', 'CURRENT_DATE'}
        for word in words:
            if word not in keywords:
                all_vars.add(word)
    
    all_vars = sorted(list(all_vars))
    
    st.write(f"**Found {len(all_vars)} unique variables**")
    
    # Create input form
    user_inputs = {}
    
    col1, col2 = st.columns(2)
    
    for idx, var in enumerate(all_vars):
        with col1 if idx % 2 == 0 else col2:
            # Check if it's a date variable
            is_date = any(keyword in var.upper() for keyword in ['DATE', 'FUP', 'TERM_START', 'SURRENDER', 'MATURITY'])
            
            if is_date:
                user_inputs[var] = st.text_input(
                    var,
                    value="2020-01-01",
                    help="Enter date in YYYY-MM-DD format"
                )
            else:
                user_inputs[var] = st.number_input(
                    var,
                    value=0.0,
                    format="%.4f",
                    help=f"Enter numeric value for {var}"
                )
    
    st.markdown("---")
    
    # Step 3: Calculate
    st.header("Step 3: Calculate Results")
    
    if st.button("🧮 Calculate All Formulas", type="primary", use_container_width=True):
        st.markdown("---")
        
        results = []
        calculation_values = user_inputs.copy()
        
        for formula in formulas_data:
            formula_name = formula['formula_name']
            mapped_expr = formula['mapped_expression']
            original_expr = formula.get('original_expression', '')
            
            st.subheader(f"📊 {formula_name}")
            
            col1, col2 = st.columns([1, 2])
            
            with col1:
                st.write("**Expression:**")
                st.code(mapped_expr, language="python")
            
            with col2:
                # Show which variables are being used
                used_vars = {}
                for var in all_vars:
                    if var in mapped_expr:
                        used_vars[var] = calculation_values.get(var, 'NOT FOUND')
                
                if used_vars:
                    st.write("**Variables used:**")
                    for var, val in used_vars.items():
                        st.text(f"{var} = {val}")
            
            # Calculate
            result = safe_eval(mapped_expr, calculation_values)
            
            if result is not None:
                st.success(f"✅ **Result: {result:,.4f}**")
                # Store result for dependent formulas
                calculation_values[formula_name] = result
                results.append({
                    'Formula': formula_name,
                    'Expression': mapped_expr,
                    'Result': result,
                    'Status': '✅ Success'
                })
            else:
                st.error(f"❌ **Result: None (calculation failed)**")
                results.append({
                    'Formula': formula_name,
                    'Expression': mapped_expr,
                    'Result': 'ERROR',
                    'Status': '❌ Failed'
                })
            
            st.markdown("---")
        
        # Summary
        st.header("📈 Summary")
        results_df = pd.DataFrame(results)
        
        success_count = sum(1 for r in results if r['Status'] == '✅ Success')
        total_count = len(results)
        
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Total Formulas", total_count)
        with col2:
            st.metric("Successful", success_count, delta=f"{success_count}/{total_count}")
        with col3:
            success_rate = (success_count / total_count * 100) if total_count > 0 else 0
            st.metric("Success Rate", f"{success_rate:.1f}%")
        
        st.dataframe(results_df, use_container_width=True, height=400)
        
        # Export results
        st.download_button(
            "📥 Download Results CSV",
            data=results_df.to_csv(index=False).encode('utf-8'),
            file_name="formula_test_results.csv",
            mime="text/csv"
        )

else:
    st.info("👆 Upload your formulas JSON file to begin testing")