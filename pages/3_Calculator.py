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

# --- Helper Functions ---
def safe_convert_to_number(value: Any) -> float:
    """Safely convert various types to float, handling dates, timestamps, etc."""
    
    # Handle None/NaN
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0.0
    
    # Handle empty strings
    if value == '' or value == ' ':
        return 0.0
    
    # Already a number
    if isinstance(value, (int, float)):
        return float(value)
    
    # Handle datetime/timestamp - convert to timestamp or year
    if isinstance(value, (datetime, date, pd.Timestamp)):
        # You can choose what makes sense for your use case:
        # Option 1: Convert to year
        return float(value.year)
        # Option 2: Convert to Unix timestamp
        # return value.timestamp()
        # Option 3: Extract days since epoch
        # return (value - datetime(1970, 1, 1)).days
    
    # Handle strings
    if isinstance(value, str):
        # Try to parse as number
        try:
            # Remove common formatting characters
            cleaned = value.replace(',', '').replace('$', '').replace('%', '').strip()
            return float(cleaned)
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
        eval_expr = eval_expr.replace('SQRT(', 'sqrt(')
        
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
        
        result = eval(eval_expr, {"__builtins__": allowed_builtins}, {})
        return result
    
    except Exception as e:
        return f"ERROR: {str(e)}"


def calculate_row(row: pd.Series, formula_expr: str, mappings: Dict) -> Any:
    """Calculate formula result for a single row"""
    
    # Build variable values from row data
    var_values = {}
    
    for var_name, mapping_obj in mappings.items():
        header = mapping_obj.mapped_header
        
        if header and header in row.index:
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
                     mappings: Dict,
                     output_columns: List[str]) -> tuple[pd.DataFrame, List[CalculationResult]]:
    """Run selected formulas on dataframe"""
    result_df = df.copy()
    calculation_results = []
    
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
    
    st.info(f"Running {len(formulas_to_run)} formula(s) for selected output columns")
    
    for formula, output_col in formulas_to_run:
        formula_name = formula.get('formula_name', 'Unknown')
        formula_expr = formula.get('formula_expression', '')
        
        errors = []
        success_count = 0
        
        # Ensure column exists in dataframe
        if output_col not in result_df.columns:
            result_df[output_col] = np.nan
        
        # Create a progress bar
        progress_text = f"Processing: {formula_name} → {output_col}"
        progress_bar = st.progress(0)
        status_text = st.empty()
        status_text.info(progress_text)
        
        total_rows = len(result_df)
        
        # Calculate for each row
        for idx, row in result_df.iterrows():
            try:
                result = calculate_row(row, formula_expr, mappings)
                
                if isinstance(result, str) and result.startswith("ERROR"):
                    errors.append(f"Row {idx}: {result}")
                    result_df.at[idx, output_col] = None  # Use None instead of ERROR string
                else:
                    result_df.at[idx, output_col] = result
                    success_count += 1
            
            except Exception as e:
                error_msg = f"Row {idx}: {str(e)}"
                errors.append(error_msg)
                result_df.at[idx, output_col] = None
            
            # Update Progress every 10 rows
            if idx % 10 == 0:
                progress_bar.progress((idx + 1) / total_rows)
                status_text.text(f"{progress_text} ({idx+1}/{total_rows})")
        
        progress_bar.empty()
        status_text.empty()
        
        success_rate = (success_count / total_rows) * 100 if total_rows > 0 else 0
        
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
    
    # Check for required session state
    if 'excel_df' not in st.session_state or st.session_state.excel_df is None:
        st.error("❌ No Excel data found. Please upload data in the previous step.")
        st.info("💡 Go back to **Variable Mapping** to upload your file.")
        return

    if 'header_to_var_mapping' not in st.session_state:
        st.error("❌ No mappings found. Please complete the mapping step first.")
        return
    
    if 'formulas' not in st.session_state or not st.session_state.formulas:
        st.error("❌ No formulas found. Please extract formulas first.")
        return
    
    # Option to reupload or use existing
    st.subheader("📊 Data Source")
    
    col_file1, col_file2 = st.columns([2, 1])
    
    with col_file1:
        use_existing = st.checkbox("Use previously uploaded Excel file", value=True)
    
    calc_df = None
    
    if not use_existing:
        st.markdown("### Upload New Excel File")
        uploaded_calc_file = st.file_uploader(
            "Upload Excel/CSV for calculations",
            type=['csv', 'xlsx', 'xls'],
            key="calc_excel_uploader"
        )
        
        if uploaded_calc_file:
            file_extension = Path(uploaded_calc_file.name).suffix.lower()
            try:
                if file_extension == '.csv':
                    calc_df = pd.read_csv(uploaded_calc_file)
                else:
                    calc_df = pd.read_excel(uploaded_calc_file)
                st.success(f"✅ Loaded {len(calc_df)} rows with {len(calc_df.columns)} columns")
            except Exception as e:
                st.error(f"Error loading file: {e}")
    else:
        calc_df = st.session_state.excel_df
        st.info(f"Using existing file with {len(calc_df)} rows and {len(calc_df.columns)} columns")
    
    if calc_df is not None:
        # Show preview
        with st.expander("📊 Data Preview (First 5 Rows)"):
            st.dataframe(calc_df.head(), use_container_width=True)
        
        st.markdown("---")
        
        # Select output columns
        st.subheader("🎯 Select Output Columns")
        st.markdown("Choose which columns should be filled with formula results. "
                    "Only formulas matching these columns will be executed.")
        
        available_cols = calc_df.columns.tolist()
        
        # Suggest columns based on formula names
        formula_names = [f.get('formula_name', '') for f in st.session_state.formulas]
        suggested_cols = []
        
        for col in available_cols:
            for fname in formula_names:
                if fname.lower() in col.lower() or col.lower() in fname.lower():
                    if col not in suggested_cols:
                        suggested_cols.append(col)
        
        if suggested_cols:
            st.info(f"💡 Suggested columns based on formula names: {', '.join(suggested_cols[:5])}")
        
        selected_output_cols = st.multiselect(
            "Output Columns",
            options=available_cols,
            default=suggested_cols[:5] if suggested_cols else [],
            help="Select columns where formula results will be written"
        )
        
        if selected_output_cols:
            st.success(f"✅ Selected {len(selected_output_cols)} output column(s)")
            
            # Show which formulas will run
            with st.expander("🔍 Preview: Formulas to be executed"):
                for formula in st.session_state.formulas:
                    fname = formula.get('formula_name', 'Unknown')
                    matched_col = match_formula_to_output_column(fname, selected_output_cols)
                    
                    if matched_col in selected_output_cols:
                        st.markdown(f"- **{fname}** → `{matched_col}`")
        
        st.markdown("---")
        
        # Run calculations button
        col_btn1, col_btn2, col_btn3 = st.columns([1, 1, 2])
        
        with col_btn1:
            run_disabled = not selected_output_cols
            
            if st.button("▶️ Run Calculations", type="primary", disabled=run_disabled):
                with st.spinner("Initializing calculation engine..."):
                    # Prepare Mappings
                    transformed_mappings = {}
                    for header, var_name in st.session_state.header_to_var_mapping.items():
                        if not header or not var_name:
                            continue
                        
                        transformed_mappings[var_name] = VariableMapping(
                            variable_name=var_name,
                            mapped_header=header,
                            confidence_score=1.0,
                            matching_method="session_state",
                            is_verified=True
                        )
                    
                    st.info(f"📊 Using {len(transformed_mappings)} variable mappings")
                    
                    formulas_to_run = st.session_state.formulas
                    
                    try:
                        result_df, calc_results = run_calculations(
                            calc_df,
                            formulas_to_run,
                            transformed_mappings,
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