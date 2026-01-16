import streamlit as st
import pandas as pd
import numpy as np
from typing import Dict, List, Any
import re
from pathlib import Path
from dataclasses import dataclass
import os
import math

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

# --- Calculation Logic ---
def safe_eval(expression: str, variables: Dict[str, Any]) -> Any:
    """Safely evaluate a mathematical expression with given variables"""
    try:
        # Replace variable names with their values
        eval_expr = expression
        
        # Sort by length descending to avoid partial replacements (e.g., 'VAR' replacing 'VAR2')
        sorted_vars = sorted(variables.keys(), key=len, reverse=True)
        
        for var_name in sorted_vars:
            # Use word boundaries to ensure exact matches
            pattern = r'\b' + re.escape(var_name) + r'\b'
            value = variables[var_name]
            
            # Handle NaN/None/Empty values
            if pd.isna(value) or value is None or value == '':
                value = 0
            else:
                # Ensure numeric type
                value = float(value)
            
            eval_expr = re.sub(pattern, str(value), eval_expr)
        
        # Handle common functions (ensure they match Python names)
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
    
    # Mappings is { "VariableName": VariableMappingObject }
    for var_name, mapping_obj in mappings.items():
        # mapping_obj.mapped_header is the Excel Column Name
        header = mapping_obj.mapped_header
        
        if header and header in row.index:
            value = row[header]
            var_values[var_name] = value
            
    # Evaluate formula with the mapped values
    result = safe_eval(formula_expr, var_values)
    return result


def run_calculations(df: pd.DataFrame, 
                     formulas: List[Dict], 
                     mappings: Dict,
                     output_columns: List[str]) -> tuple[pd.DataFrame, List[CalculationResult]]:
    """
    Run all formulas on dataframe
    """
    result_df = df.copy()
    calculation_results = []
    
    for formula in formulas:
        formula_name = formula.get('formula_name', 'Unknown')
        formula_expr = formula.get('formula_expression', '')
        
        errors = []
        success_count = 0
        
        # Determine output column for this formula
        output_col = None
        for col in output_columns:
            if col.lower() in formula_name.lower() or formula_name.lower() in col.lower():
                output_col = col
                break
        
        if not output_col:
            output_col = formula_name
        
        # Ensure column exists in dataframe
        if output_col not in result_df.columns:
            result_df[output_col] = np.nan
        
        # Create a progress bar for this formula's execution
        progress_text = f"Processing: {formula_name}..."
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
                    result_df.at[idx, output_col] = result
                else:
                    result_df.at[idx, output_col] = result
                    success_count += 1
            
            except Exception as e:
                error_msg = f"Row {idx}: {str(e)}"
                errors.append(error_msg)
                result_df.at[idx, output_col] = f"ERROR"
                
            # Update Progress
            if idx % 10 == 0: # Update every 10 rows to save performance
                progress_bar.progress((idx + 1) / total_rows)
                status_text.text(f"{progress_text} ({idx+1}/{total_rows})")
        
        progress_bar.empty()
        status_text.empty()
        
        success_rate = (success_count / total_rows) * 100 if total_rows > 0 else 0
        
        calculation_results.append(CalculationResult(
            formula_name=formula_name,
            rows_calculated=success_count,
            errors=errors[:10],  # Limit to first 10 errors
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
    
    # Check for required session state
    if 'excel_df' not in st.session_state or st.session_state.excel_df is None:
        st.error("❌ No Excel data found. Please upload data in the previous step.")
        st.info("💡 Go back to **Variable Mapping** to upload your file.")
        return

    if 'header_to_var_mapping' not in st.session_state:
        st.error("❌ No mappings found. Please complete the mapping step first.")
        return
    
    # Option to reupload or use existing
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
                st.success(f"✅ Loaded {len(calc_df)} rows")
            except Exception as e:
                st.error(f"Error loading file: {e}")
    else:
        calc_df = st.session_state.excel_df
        st.info(f"Using existing file with {len(calc_df)} rows")
    
    if calc_df is not None:
        # Show preview
        with st.expander("📊 Data Preview"):
            st.dataframe(calc_df.head(), use_container_width=True)
        
        st.markdown("---")
        
        # Select output columns
        st.markdown("### Select Output Columns to Populate")
        st.markdown("Choose which columns should be filled with formula results. "
                    "The system will try to match formula names to column names automatically.")
        
        available_cols = calc_df.columns.tolist()
        selected_output_cols = st.multiselect(
            "Output Columns",
            options=available_cols,
            help="Select columns where formula results will be written"
        )
        
        if selected_output_cols:
            st.info(f"Selected {len(selected_output_cols)} output column(s)")
        
        st.markdown("---")
        
        # Run calculations button
        col_btn1, col_btn2 = st.columns([1, 3])
        
        with col_btn1:
            if st.button("▶️ Run Calculations", type="primary", disabled=not selected_output_cols):
                with st.spinner("Initializing calculation engine..."):
                    # 1. Prepare Mappings
                    # Session state has { "Excel_Header": "VariableName" }
                    # Calculation logic needs { "VariableName": VariableMappingObject }
                    
                    transformed_mappings = {}
                    for header, var_name in st.session_state.header_to_var_mapping.items():
                        if not header or not var_name:
                            continue
                        
                        # Create a mapping object expected by calculate_row
                        transformed_mappings[var_name] = VariableMapping(
                            variable_name=var_name,
                            mapped_header=header,
                            confidence_score=1.0,
                            matching_method="session_state",
                            is_verified=True
                        )
                    
                    # Debugging info: Show how many variables we have mapped
                    st.info(f"Found {len(transformed_mappings)} variable mappings ready for calculation.")
                    
                    # 2. Run Logic
                    formulas_to_run = st.session_state.formulas
                    
                    if not formulas_to_run:
                        st.error("No formulas found in session.")
                    
                    try:
                        result_df, calc_results = run_calculations(
                            calc_df,
                            formulas_to_run,
                            transformed_mappings,
                            selected_output_cols
                        )
                        
                        # Store results in session state for download/display
                        st.session_state.results_df = result_df
                        st.session_state.calc_results = calc_results
                        
                        st.success("✅ Calculations complete!")
                        st.rerun()
                    
                    except Exception as e:
                        st.error(f"❌ Calculation Error: {e}")
                        import traceback
                        st.error(traceback.format_exc())
    
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
            with st.expander(f"**{calc_result.formula_name}** - {calc_result.success_rate:.1f}% success"):
                st.markdown(f"**Rows Calculated:** {calc_result.rows_calculated} / {total_rows}")
                
                if calc_result.errors:
                    st.markdown("**Errors:**")
                    for error in calc_result.errors:
                        st.error(error)
        
        # Show results dataframe
        st.markdown("---")
        st.markdown("### Results Data")
        st.dataframe(st.session_state.results_df, use_container_width=True)
        
        # Export options
        st.markdown("---")
        col_exp1, col_exp2, col_exp3 = st.columns([1, 1, 2])
        
        with col_exp1:
            # Export to Excel
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
            # Export to CSV
            csv_data = st.session_state.results_df.to_csv(index=False)
            st.download_button(
                label="📥 Download CSV",
                data=csv_data,
                file_name="calculation_results.csv",
                mime="text/csv"
            )
        
        with col_exp3:
            if st.button("🔄 Start New Calculation"):
                st.session_state.results_df = None
                st.session_state.calc_results = None
                st.rerun()

if __name__ == "__main__":
    main()