import os
import re
import json
import tempfile
from typing import List, Dict, Tuple, Optional, Set
import streamlit as st
from dotenv import load_dotenv
from dataclasses import dataclass, asdict
from openai import AzureOpenAI
import pandas as pd
import numpy as np
from rapidfuzz import fuzz
from difflib import SequenceMatcher
import openpyxl
from pathlib import Path

load_dotenv()

# Configuration
ALLOWED_EXCEL_EXTENSIONS = {'xlsx', 'xls', 'csv'}
MAX_FILE_SIZE = 16 * 1024 * 1024  # 16MB

# Azure OpenAI Configuration (for AI semantic matching only)
AZURE_API_KEY = os.getenv('AZURE_OPENAI_API_KEY')
AZURE_ENDPOINT = os.getenv('AZURE_OPENAI_ENDPOINT')
DEPLOYMENT_NAME = os.getenv('AZURE_OPENAI_DEPLOYMENT_NAME')
AZURE_API_VERSION = os.getenv('AZURE_OPENAI_API_VERSION')

if AZURE_API_KEY:
    try:
        client = AzureOpenAI(
            azure_endpoint=AZURE_ENDPOINT,
            api_key=AZURE_API_KEY,
            api_version=AZURE_API_VERSION
        )
        MOCK_MODE = False
    except Exception as e:
        st.error(f"❌ Failed to initialize Azure OpenAI client: {str(e)}")
        MOCK_MODE = True
        client = None
else:
    MOCK_MODE = True
    client = None

# Confidence thresholds
CONFIDENCE_THRESHOLDS = {
    'high': 0.85,
    'medium': 0.60,
    'low': 0.40
}
# --- INPUT VARIABLES DEFINITIONS ---
INPUT_VARIABLES = {
    'TERM_START_DATE': 'Date when the policy starts',
    'FUP_Date': 'First Unpaid Premium date',
    'ENTRY_AGE': 'Age of the policyholder at policy inception',
    'TOTAL_PREMIUM': 'Annual Premium amount',
    'BOOKING_FREQUENCY': 'Frequency of premium booking (monthly, quarterly, yearly)',
    'PREMIUM_TERM': 'Premium Paying Term - duration for paying premiums',
    'SUM_ASSURED': 'Sum Assured - guaranteed amount on maturity/death',
    'Income_Benefit_Amount': 'Amount of income benefit',
    'Income_Benefit_Frequency': 'Frequency of income benefit payout',
    'DATE_OF_SURRENDER': 'Date when policy is surrendered',
    'no_of_premium_paid': 'Years passed since date of commencement till FUP',
    'maturity_date': 'Date of commencement + (BENEFIT_TERM * 12 months)',
    'policy_year': 'Years passed + 1 between date of commencement and surrender date',
    'BENEFIT_TERM': 'The duration (in years) for which the policy benefits are payable',
    'GSV_FACTOR': 'Guaranteed Surrender Value Factor',
    'SSV1_FACTOR': 'Surrender Value Factor',
    'SSV3_FACTOR': 'Special Surrender Value Factor for paid-up income benefits',
    'SSV2_FACTOR': 'Special Surrender Value Factor for return of premium',
    'FUND_VALUE': 'The total value of the policy fund at surrender or maturity',
    'N': 'min(Policy_term, 20) - Elapsed_policy_duration',
    'SYSTEM_PAID': 'Amount paid by system for surrender or maturity',
    'CAPITAL_UNITS_VALUE': 'Number of units in policy fund at surrender or maturity',
}

BASIC_DERIVED_FORMULAS = {
    'no_of_premium_paid': 'Calculate based on difference between TERM_START_DATE and FUP_Date',
    'policy_year': 'Calculate based on difference between TERM_START_DATE and DATE_OF_SURRENDER + 1',
    'maturity_date': 'TERM_START_DATE + (BENEFIT_TERM* 12) months',
    'Final_surrender_value_paid': 'Final surrender value paid',
    'Elapsed_policy_duration': 'How many years have passed since policy start',
    'CAPITAL_FUND_VALUE': 'Total policy fund value including bonuses',
    'FUND_FACTOR': 'Factor to compute fund value based on premiums and term'
}

DEFAULT_TARGET_OUTPUT_VARIABLES = [
    'TOTAL_PREMIUM_PAID', 'TEN_TIMES_AP', 'one_oh_five_percent_total_premium',
    'SUM_ASSURED_ON_DEATH', 'SUM_ASSURED', 'GSV', 'PAID_UP_SA',
    'PAID_UP_SA_ON_DEATH', 'paid_up_income_benefit_amount',
    'SSV1_AMT', 'SSV2_AMT', 'SSV3_AMT', 'SSV', 'SURRENDER_PAID_AMOUNT',
]

@dataclass
class VariableMapping:
    variable_name: str
    mapped_header: str
    confidence_score: float
    matching_method: str
    is_verified: bool = False
    
    def to_dict(self):
        return asdict(self)

class VariableHeaderMatcher:
    """Matches formula variables to Excel headers using multiple strategies"""
    
    def __init__(self):
        self.mappings: Dict[str, VariableMapping] = {}
        
    def normalize_text(self, text: str) -> str:
        """Normalize text for comparison"""
        # Convert to lowercase, remove special chars, collapse whitespace
        text = text.lower()
        text = re.sub(r'[^a-z0-9\s]', ' ', text)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()
    
    def expand_abbreviations(self, text: str) -> str:
        """Expand common insurance/financial abbreviations"""
        abbreviations = {
            'sa': 'sum assured',
            'dob': 'date of birth',
            'fup': 'first unpaid premium',
            'gsv': 'guaranteed surrender value',
            'ssv': 'special surrender value',
            'rop': 'return of premium',
            'ap': 'annual premium',
            'pp': 'premium paid',
            'amt': 'amount',
            'calc': 'calculation',
            'freq': 'frequency',
            'mat': 'maturity',
            'ben': 'benefit',
            'dt': 'date',
            'no': 'number',
            'yr': 'year',
            'yrs': 'years',
            'pct': 'percent',
            'val': 'value',
            'pd': 'paid',
            'term': 'term',
            'pol': 'policy',
            'prem': 'premium',
            'tot': 'total'
        }
        
        text_lower = text.lower()
        for abbr, full in abbreviations.items():
            # Replace whole word abbreviations
            text_lower = re.sub(r'\b' + abbr + r'\b', full, text_lower)
        
        return text_lower
    
    def lexical_similarity(self, var: str, header: str) -> float:
        """Calculate lexical similarity using multiple methods"""
        var_norm = self.normalize_text(var)
        header_norm = self.normalize_text(header)
        
        # Expand abbreviations for better matching
        var_expanded = self.expand_abbreviations(var_norm)
        header_expanded = self.expand_abbreviations(header_norm)
        
        # Exact match
        if var_expanded == header_expanded:
            return 1.0
        
        # Substring match
        if var_expanded in header_expanded or header_expanded in var_expanded:
            return 0.95
        
        # Token-based matching
        var_tokens = set(var_expanded.split())
        header_tokens = set(header_expanded.split())
        
        if var_tokens and header_tokens:
            intersection = len(var_tokens & header_tokens)
            union = len(var_tokens | header_tokens)
            jaccard = intersection / union if union > 0 else 0
            
            # If all variable tokens are in header, give high score
            if var_tokens.issubset(header_tokens):
                return 0.90
            
            return jaccard * 0.85
        
        return 0.0
    
    def fuzzy_similarity(self, var: str, header: str) -> float:
        """Calculate fuzzy string similarity"""
        var_norm = self.normalize_text(var)
        header_norm = self.normalize_text(header)
        
        # Expand abbreviations
        var_expanded = self.expand_abbreviations(var_norm)
        header_expanded = self.expand_abbreviations(header_norm)
        
        # rapidfuzz returns 0-100, so divide by 100
        ratio = fuzz.ratio(var_expanded, header_expanded) / 100.0
        partial_ratio = fuzz.partial_ratio(var_expanded, header_expanded) / 100.0
        token_sort_ratio = fuzz.token_sort_ratio(var_expanded, header_expanded) / 100.0
        token_set_ratio = fuzz.token_set_ratio(var_expanded, header_expanded) / 100.0
        
        # Weighted average favoring token-based matching
        score = (ratio * 0.2 + partial_ratio * 0.2 + token_sort_ratio * 0.3 + token_set_ratio * 0.3)
        return score
    
    def semantic_similarity_ai(self, var: str, header: str) -> Tuple[float, str]:
        """Calculate semantic similarity using AI"""
        if MOCK_MODE or not client:
            return 0.0, "AI unavailable"
        
        try:
            prompt = f"""Compare these two terms and rate their semantic similarity on a scale of 0.0 to 1.0:

Variable: "{var}"
Header: "{header}"

Consider:
- Do they refer to the same concept?
- Are they synonyms or related terms?
- In an insurance/financial context, would they represent the same data?

Respond with ONLY a number between 0.0 and 1.0, followed by a brief explanation.
Format: SCORE: X.XX | REASON: explanation"""

            response = client.chat.completions.create(
                model=DEPLOYMENT_NAME,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=150,
                temperature=0.1
            )
            
            response_text = response.choices[0].message.content.strip()
            
            # Parse response
            score_match = re.search(r'SCORE:\s*([0-9]*\.?[0-9]+)', response_text, re.IGNORECASE)
            reason_match = re.search(r'REASON:\s*(.+)', response_text, re.IGNORECASE | re.DOTALL)
            
            score = float(score_match.group(1)) if score_match else 0.0
            reason = reason_match.group(1).strip() if reason_match else "No explanation provided"
            
            return min(score, 1.0), f"semantic_ai ({reason[:50]}...)"
            
        except Exception as e:
            st.warning(f"AI semantic matching failed: {e}")
            return 0.0, f"AI_error: {str(e)}"
    
    def find_best_match(self, variable: str, headers: List[str], use_ai: bool = False) -> Optional[VariableMapping]:
        """Find the best matching header for a variable"""
        best_score = 0.0
        best_header = None
        best_method = "no_match"
        
        # Stage 1: Lexical matching
        for header in headers:
            lex_score = self.lexical_similarity(variable, header)
            
            if lex_score > best_score:
                best_score = lex_score
                best_header = header
                best_method = "lexical"
        
        # Stage 2: Fuzzy matching
        for header in headers:
            fuzzy_score = self.fuzzy_similarity(variable, header)
            
            if fuzzy_score > best_score:
                best_score = fuzzy_score
                best_header = header
                best_method = "fuzzy"
        
        # Stage 3: AI semantic matching (only if enabled)
        if use_ai and best_header:
            ai_score, ai_method = self.semantic_similarity_ai(variable, best_header)
            
            if ai_score > best_score:
                best_score = ai_score
                best_method = ai_method
        
        if best_header and best_score >= CONFIDENCE_THRESHOLDS['low']:
            return VariableMapping(
                variable_name=variable,
                mapped_header=best_header,
                confidence_score=best_score,
                matching_method=best_method,
                is_verified=best_score >= CONFIDENCE_THRESHOLDS['high']
            )
        
        return None
    
    def match_all_variables(self, variables: List[str], headers: List[str], use_ai: bool = False) -> Dict[str, VariableMapping]:
        """Match all variables to headers, including ALL Excel headers"""
        mappings = {}
        
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        # Combine extracted variables with predefined INPUT_VARIABLES
        all_vars_to_match = set(variables) | set(INPUT_VARIABLES.keys())
        total_vars = len(all_vars_to_match)
        
        for idx, var in enumerate(all_vars_to_match):
            progress = (idx + 1) / total_vars
            progress_bar.progress(progress)
            status_text.text(f"Matching variable {idx + 1}/{total_vars}: {var}")
            
            mapping = self.find_best_match(var, headers, use_ai=use_ai)
            if mapping:
                mappings[var] = mapping
            else:
                mappings[var] = VariableMapping(
                    variable_name=var,
                    mapped_header="",
                    confidence_score=0.0,
                    matching_method="no_match",
                    is_verified=False
                )
        
        # Add all Excel headers that aren't mapped yet
        mapped_headers = {m.mapped_header for m in mappings.values() if m.mapped_header}
        for header in headers:
            if header not in mapped_headers:
                # Create a reverse mapping entry
                mappings[f"_header_{header}"] = VariableMapping(
                    variable_name=header,
                    mapped_header=header,
                    confidence_score=0.0,
                    matching_method="unmapped_header",
                    is_verified=False
                )
        
        progress_bar.empty()
        status_text.empty()
        
        return mappings


def extract_variables_from_formulas(formulas: List[Dict]) -> Tuple[Set[str], Dict[str, str]]:
    """Extract all unique variables from formula expressions AND calculation steps
    Returns: (all_variables, derived_variables_mapping)
    """
    variables = set()
    derived_vars = {}  # Maps derived var to its formula
    
    # Pattern to match variable names
    var_pattern = r'\b([a-zA-Z][a-zA-Z0-9_]*)\b'
    
    for formula in formulas:
        # Extract from main formula expression
        expr = formula.get('formula_expression', '')
        matches = re.findall(var_pattern, expr)
        variables.update(matches)
        
        # Extract from calculation steps (intermediate variables created by AI)
        calc_steps = formula.get('calculation_steps', [])
        if isinstance(calc_steps, list):
            for step in calc_steps:
                if isinstance(step, dict):
                    step_text = step.get('step', '') + ' ' + step.get('formula', '')
                    step_formula = step.get('formula', '')
                    step_matches = re.findall(var_pattern, step_text)
                    variables.update(step_matches)
                    
                    # Try to identify derived variable definitions (e.g., "variable_name = expression")
                    derived_match = re.match(r'([a-zA-Z][a-zA-Z0-9_]*)\s*=\s*(.+)', step_formula)
                    if derived_match:
                        var_name = derived_match.group(1)
                        var_formula = derived_match.group(2)
                        derived_vars[var_name] = var_formula
                        
                elif isinstance(step, str):
                    step_matches = re.findall(var_pattern, step)
                    variables.update(step_matches)
                    
                    # Try to identify derived variable definitions
                    derived_match = re.match(r'([a-zA-Z][a-zA-Z0-9_]*)\s*=\s*(.+)', step)
                    if derived_match:
                        var_name = derived_match.group(1)
                        var_formula = derived_match.group(2)
                        derived_vars[var_name] = var_formula
    
    # Expanded filter list
    operators = {
        'MAX', 'MIN', 'SUM', 'AVG', 'AVERAGE', 'IF', 'THEN', 'ELSE', 'AND', 'OR', 'NOT',
        'ROUND', 'CEILING', 'FLOOR', 'ABS', 'POWER', 'SQRT', 'MOD', 'INT',
        'COUNT', 'VLOOKUP', 'INDEX', 'MATCH', 'ISERROR', 'ISBLANK',
        'TRUE', 'FALSE', 'NA', 'PI', 'EXP', 'LN', 'LOG', 'LOG10',
        'SIN', 'COS', 'TAN', 'ASIN', 'ACOS', 'ATAN',
        'CONCATENATE', 'LEFT', 'RIGHT', 'MID', 'LEN', 'TRIM',
        'UPPER', 'LOWER', 'PROPER', 'SUBSTITUTE', 'REPLACE',
        'TODAY', 'NOW', 'YEAR', 'MONTH', 'DAY', 'HOUR', 'MINUTE', 'SECOND',
        'DATE', 'TIME', 'DATEVALUE', 'TIMEVALUE',
        'COUNTIF', 'SUMIF', 'AVERAGEIF', 'MAXIFS', 'MINIFS',
        'LOOKUP', 'HLOOKUP', 'CHOOSE', 'OFFSET',
        'for', 'while', 'do', 'break', 'continue', 'return',
        'def', 'class', 'import', 'from', 'as', 'with',
        'try', 'except', 'finally', 'raise', 'assert',
        'in', 'is', 'None', 'True', 'False',
        'div', 'mod', 'abs', 'max', 'min', 'sum', 'avg',
        'Step', 'Calculate', 'Formula', 'Result', 'Value'
    }
    
    operators_normalized = {op.upper() for op in operators} | {op.lower() for op in operators}
    
    # Filter out operators and purely numeric strings
    variables = {v for v in variables if v not in operators_normalized and not v.isdigit()}
    
    return variables, derived_vars

def load_excel_file(file_bytes, file_extension: str) -> Tuple[pd.DataFrame, List[str]]:
    """Load Excel file and extract headers"""
    try:
        if file_extension == '.csv':
            df = pd.read_csv(pd.io.common.BytesIO(file_bytes))
        else:
            df = pd.read_excel(pd.io.common.BytesIO(file_bytes))
        
        # Get headers (column names)
        headers = df.columns.tolist()
        
        return df, headers
    
    except Exception as e:
        st.error(f"Error loading Excel file: {e}")
        return None, []


def apply_mappings_to_formulas(formulas: List[Dict], mappings: Dict[str, VariableMapping]) -> List[Dict]:
    """Replace variables in formulas with mapped headers"""
    mapped_formulas = []
    
    for formula in formulas:
        expr = formula.get('formula_expression', '')
        
        # Replace each variable with its mapped header
        for var_name, mapping in mappings.items():
            if mapping.mapped_header and mapping.is_verified:
                # Use word boundaries to avoid partial replacements
                pattern = r'\b' + re.escape(var_name) + r'\b'
                expr = re.sub(pattern, f'[{mapping.mapped_header}]', expr)
        
        mapped_formulas.append({
            'formula_name': formula.get('formula_name', ''),
            'original_expression': formula.get('formula_expression', ''),
            'mapped_expression': expr
        })
    
    return mapped_formulas
def show_calculation_results():
    """Display calculation results"""
    st.subheader("✅ Calculation Results")
    
    # Summary statistics
    st.markdown("### Summary")
    
    col1, col2, col3 = st.columns(3)
    
    total_rows = len(st.session_state.results_df)
    total_formulas = len(st.session_state.calc_results)
    
    avg_success = sum(r.success_rate for r in st.session_state.calc_results) / total_formulas if total_formulas > 0 else 0
    
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
            st.session_state.calculation_complete = False
            st.session_state.results_df = None
            st.session_state.calc_results = None
            st.rerun()
def show_calculation_engine():
    """Display calculation engine interface"""
    st.subheader("🧮 Calculation Engine")
    st.markdown("Apply formulas to your data row-by-row and generate calculated results.")
    
    # Option to reupload or use existing
    col_file1, col_file2 = st.columns([2, 1])
    
    with col_file1:
        use_existing = st.checkbox("Use previously uploaded Excel file", value=True)
    
    calc_df = None
    
    if not use_existing:
        st.markdown("### Upload New Excel File")
        uploaded_calc_file = st.file_uploader(
            "Upload Excel/CSV for calculations",
            type=list(ALLOWED_EXCEL_EXTENSIONS),
            key="calc_excel_uploader"
        )
        
        if uploaded_calc_file:
            file_extension = Path(uploaded_calc_file.name).suffix.lower()
            calc_df, calc_headers = load_excel_file(uploaded_calc_file.read(), file_extension)
            
            if calc_df is not None:
                st.success(f"✅ Loaded {len(calc_df)} rows")
                
                # Verify headers match mappings
                mapped_headers = set(st.session_state.header_to_var_mapping.keys())
                file_headers = set(calc_headers)
                
                if not mapped_headers.issubset(file_headers):
                    missing = mapped_headers - file_headers
                    st.error(f"❌ Missing headers in new file: {', '.join(list(missing)[:5])}")
                    calc_df = None
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
        st.markdown("Choose which columns should be filled with formula results")
        
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
                with st.spinner("Calculating..."):
                    # Import calculation engine
                    from calculation_engine import run_calculations
                    
                    # Run calculations
                    result_df, calc_results = run_calculations(
                        calc_df,
                        st.session_state.formulas,
                        st.session_state.variable_mappings,
                        selected_output_cols
                    )
                    
                    # Store results
                    st.session_state.results_df = result_df
                    st.session_state.calc_results = calc_results
                    st.session_state.calculation_complete = True
                    
                    st.success("✅ Calculations complete!")
                    st.rerun()

def show_calculation_results():
    """Display calculation results"""
    st.subheader("✅ Calculation Results")
    
    # Summary statistics
    st.markdown("### Summary")
    
    col1, col2, col3 = st.columns(3)
    
    total_rows = len(st.session_state.results_df)
    total_formulas = len(st.session_state.calc_results)
    
    avg_success = sum(r.success_rate for r in st.session_state.calc_results) / total_formulas if total_formulas > 0 else 0
    
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
            st.session_state.calculation_complete = False
            st.session_state.results_df = None
            st.session_state.calc_results = None
            st.rerun()
def set_custom_css():
    """Apply custom CSS"""
    st.markdown(
        """
        <style>
       @import url('https://fonts.googleapis.com/css2?family=Merriweather:wght@700&display=swap');
       @import url('https://fonts.googleapis.com/css2?family=Open+Sans:wght@400;600&display=swap');

        html, body, .main {
            font-family: 'Roboto', sans-serif;
            background: linear-gradient(135deg, #a6d3ff 50%, #cbdff7 100%);
            color: #2c3e50;
            animation: fadeIn 1s ease-in-out;
            overflow-x: hidden;
        }

        @keyframes fadeIn {
            from { opacity: 0; }
            to { opacity: 1; }
        }

        .stButton > button {
            background: linear-gradient(135deg, #a6d3ff 50%, #cbdff7 100%);
            color: white !important;
            padding: 12px 25px;
            border-radius: 10px;
            border: none;
            cursor: pointer;
            font-size: 17px;
            font-weight: 600;
            transition: all 0.3s ease;
            box-shadow: 0 4px 12px rgba(0, 77, 168, 0.3);
            letter-spacing: 0.5px;
        }
        .stButton > button:hover {
            background: linear-gradient(135deg,  #004DA8 25%, #1e88e5 50%);
            transform: translateY(-3px) scale(1.02);
            box-shadow: 0 6px 18px rgba(0, 77, 168, 0.4);
            color: white !important;
        }

        .streamlit-expander > div[role="button"] {
            background: linear-gradient(135deg, #e3f2fd 0%, #bbdefb 100%) !important;
            border-left: 6px solid #004DA8 !important;
            padding: 12px 15px;
            border-radius: 10px;
            margin-bottom: 8px;
            transition: all 0.3s ease;
            box-shadow: 0 2px 5px rgba(0, 77, 168, 0.1);
            font-weight: 500;
            color: #1a1a1a !important;
            font-family: 'Montserrat', sans-serif;
            font-size: 1.05em;
        }

        h1, h2, h3, h4, h5, h6 {
            font-family: 'Montserrat', sans-serif !important;
            color: #004DA8 !important;
            font-weight: 700;
        }

        .stDataFrame table {
            font-family: 'Roboto', sans-serif;
            border-collapse: collapse;
            box-shadow: 0 5px 15px rgba(0, 77, 168, 0.1);
            border-radius: 12px;
            overflow: hidden;
            background-color: white;
        }
        
        .stDataFrame thead th {
            background: linear-gradient(135deg, #004DA8 0%, #1976d2 100%);
            color: white !important;
            padding: 15px 20px;
            font-weight: 700;
        }

        .confidence-high {
            background-color: #d4edda !important;
            color: #155724 !important;
            padding: 4px 8px;
            border-radius: 4px;
            font-weight: 600;
        }

        .confidence-medium {
            background-color: #fff3cd !important;
            color: #856404 !important;
            padding: 4px 8px;
            border-radius: 4px;
            font-weight: 600;
        }

        .confidence-low {
            background-color: #f8d7da !important;
            color: #721c24 !important;
            padding: 4px 8px;
            border-radius: 4px;
            font-weight: 600;
        }

        .header-unmapped {
            background-color: #e8f4f8 !important;
            color: #0277bd !important;
            padding: 4px 8px;
            border-radius: 4px;
            font-style: italic;
        }
        </style>
        """,
        unsafe_allow_html=True
    )


def main():
    st.set_page_config(
        page_title="Variable Mapping",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    
    set_custom_css()
    
    # Custom header
    st.markdown(
        """
        <div class="header-container">
            <div class="header-bar">
                <img src="https://raw.githubusercontent.com/AyushiR0y/streamlit_formulagen/main/assets/logo.png" style="height: 100px;">
                <div class="header-title" style="font-size: 2.5rem; font-weight: 750; color: #004DA8;">
                    Formula Calculation - Variable Mapping
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )
    
    st.markdown("---")
    
    # Initialize session state
    if 'formulas' not in st.session_state or not st.session_state.formulas:
        st.error("❌ No formulas found. Please go back to the extraction page and extract formulas first.")
        st.info("💡 Use the sidebar navigation to return to the main page.")
        return
    
    if 'excel_headers' not in st.session_state:
        st.session_state.excel_headers = []
    
    if 'variable_mappings' not in st.session_state:
        st.session_state.variable_mappings = {}
    
    if 'mapping_complete' not in st.session_state:
        st.session_state.mapping_complete = False
    
    if 'excel_df' not in st.session_state:
        st.session_state.excel_df = None
    
    if 'initial_mapping_done' not in st.session_state:
        st.session_state.initial_mapping_done = False
    
    if 'custom_formulas' not in st.session_state:
        st.session_state.custom_formulas = []
    
    if 'derived_variables' not in st.session_state:
        st.session_state.derived_variables = {}
    if 'calculation_complete' not in st.session_state:
        st.session_state.calculation_complete = False

    if 'results_df' not in st.session_state:
        st.session_state.results_df = None

    if 'calc_results' not in st.session_state:
        st.session_state.calc_results = None
    
   # Check if we're in calculation results mode
    if st.session_state.mapping_complete and st.session_state.calculation_complete:
        show_calculation_results()
        return

    # Check if we're ready for calculations
    if st.session_state.mapping_complete:
        show_calculation_engine()
        return

    # Check if formulas exist
    if not st.session_state.formulas:
        st.error("❌ No formulas found in session.")
        st.warning("⚠️ **Note:** Refreshing this page will clear your session. Use the navigation menu instead of browser refresh.")
        st.info("💡 Use the sidebar navigation to return to the main page and extract formulas again.")
        
        # Option to upload previously saved mappings
        st.markdown("---")
        st.subheader("📥 Restore Previous Session")
        uploaded_json = st.file_uploader("Upload previously exported mappings JSON", type=['json'])
        if uploaded_json:
            try:
                import_data = json.loads(uploaded_json.read())
                if 'formulas' in import_data:
                    st.session_state.formulas = import_data['formulas']
                    st.success("✅ Formulas restored!")
                    st.rerun()
            except Exception as e:
                st.error(f"Error loading file: {e}")
        return

    # Main content
    col1, col2 = st.columns([1, 1])
    
    with col1:
        st.subheader("📤 Upload Excel Data File")
        st.markdown("Upload your Excel file containing the policy data that will be used for calculations.")
        
        uploaded_file = st.file_uploader(
            "Select Excel/CSV file",
            type=list(ALLOWED_EXCEL_EXTENSIONS),
            help=f"Accepts: {', '.join(ALLOWED_EXCEL_EXTENSIONS)}. Max file size: {MAX_FILE_SIZE / (1024*1024):.1f} MB",
            key="excel_uploader"
        )
        
        if uploaded_file is not None:
            if uploaded_file.size > MAX_FILE_SIZE:
                st.error(f"File size exceeds the limit. Please upload a file smaller than {MAX_FILE_SIZE / (1024*1024):.1f} MB.")
            else:
                st.info(f"**File Selected:** `{uploaded_file.name}` (`{uploaded_file.size / 1024:.1f} KB`)")
                
                # Load Excel file
                file_extension = Path(uploaded_file.name).suffix.lower()
                df, headers = load_excel_file(uploaded_file.read(), file_extension)
                
                if df is not None and headers:
                    st.session_state.excel_df = df
                    st.session_state.excel_headers = headers
                    
                    st.success(f"✅ Successfully loaded {len(df)} rows with {len(headers)} columns")
                    
                    # Show preview
                    with st.expander("📊 Data Preview", expanded=False):
                        st.dataframe(df.head(10), use_container_width=True)
                    
                    # Start mapping process
                    st.markdown("---")
                    
                    st.subheader("⚙️ Initial Matching (Lexical + Fuzzy)")
                    
                    st.info("🔄 **Strategy**: Uses fast lexical and fuzzy matching without AI")
                    
                    # Extract variables first to show user what will be matched
                    all_variables, derived_vars = extract_variables_from_formulas(st.session_state.formulas)
                    st.session_state.derived_variables = derived_vars
                    
                    # Add custom formulas to variables
                    for custom_formula in st.session_state.custom_formulas:
                        custom_vars, custom_derived = extract_variables_from_formulas([custom_formula])
                        all_variables.update(custom_vars)
                        st.session_state.derived_variables.update(custom_derived)
                    
                    if st.button("🔗 Start Automatic Mapping", type="primary", disabled=st.session_state.initial_mapping_done):
                        with st.spinner("Analyzing variables and matching with headers..."):
                            matcher = VariableHeaderMatcher()
                            mappings = matcher.match_all_variables(
                                list(all_variables),
                                headers,
                                use_ai=False  # No AI in initial pass
                            )
                            st.session_state.variable_mappings = mappings
                            st.session_state.initial_mapping_done = True
                            
                            # Show matching statistics
                            total = len(mappings)
                            mapped = len([m for m in mappings.values() if m.mapped_header])
                            
                            # Count by method
                            method_counts = {}
                            for m in mappings.values():
                                if m.mapped_header:
                                    method = m.matching_method.split('_')[0]
                                    method_counts[method] = method_counts.get(method, 0) + 1
                            
                            st.success(f"✅ Mapped {mapped} out of {total} variables")
                            
                            if method_counts:
                                st.markdown("**Matching Methods Used:**")
                                method_df = pd.DataFrame([
                                    {"Method": method.title(), "Count": count}
                                    for method, count in sorted(method_counts.items(), key=lambda x: -x[1])
                                ])
                                st.dataframe(method_df, hide_index=True, use_container_width=True)
                            
                            st.rerun()
    
    with col2:
        st.subheader("📋 Extracted Variables")
        st.markdown("Variables from formulas **and** AI-generated calculation steps.")
        
        # Extract variables from formulas
        all_variables, derived_vars = extract_variables_from_formulas(st.session_state.formulas)
        st.session_state.derived_variables = derived_vars
        
        # Add custom formula variables
        for custom_formula in st.session_state.custom_formulas:
            custom_vars, custom_derived = extract_variables_from_formulas([custom_formula])
            all_variables.update(custom_vars)
            st.session_state.derived_variables.update(custom_derived)
        
        if all_variables:
            # Separate into input and derived variables
            input_vars = [v for v in all_variables if v not in st.session_state.derived_variables]
            derived_var_list = [v for v in all_variables if v in st.session_state.derived_variables]
            
            var_df_data = []
            for var in sorted(input_vars):
                var_df_data.append({'Variable Name': var, 'Type': 'Input'})
            for var in sorted(derived_var_list):
                var_df_data.append({'Variable Name': var, 'Type': 'Derived'})
            
            var_df = pd.DataFrame(var_df_data)
            st.dataframe(var_df, use_container_width=True, hide_index=True)
            
            # Show derived variable formulas
            if st.session_state.derived_variables:
                with st.expander("📐 Derived Variable Formulas", expanded=False):
                    for var, formula in sorted(st.session_state.derived_variables.items()):
                        st.markdown(f"**`{var}`** = `{formula}`")
            
            # Show which formulas each variable appears in
            with st.expander("🔍 Variable Usage Details", expanded=False):
                for var in sorted(all_variables):
                    formulas_with_var = [
                        f.get('formula_name', 'Unknown')
                        for f in st.session_state.formulas 
                        if re.search(r'\b' + re.escape(var) + r'\b', 
                                   str(f.get('formula_expression', '')) + ' ' + 
                                   str(f.get('calculation_steps', '')))
                    ]
                    # Also check custom formulas
                    for cf in st.session_state.custom_formulas:
                        if re.search(r'\b' + re.escape(var) + r'\b', 
                                   str(cf.get('formula_expression', ''))):
                            formulas_with_var.append(cf.get('formula_name', 'Custom'))
                    
                    if formulas_with_var:
                        var_type = "Derived" if var in st.session_state.derived_variables else "Input"
                        st.markdown(f"**`{var}`** ({var_type}): {len(formulas_with_var)} formula(s)")
                        st.caption(", ".join(formulas_with_var))
        else:
            st.info("No variables detected in formulas.")
        
        # Replace the entire "Add custom formula section" block with:
        st.markdown("---")
        with st.expander("➕ Add Custom Formula", expanded=False):
            custom_name = st.text_input("Name", placeholder="Custom_Calc", key="cf_name")
            custom_expr = st.text_input("Expression", placeholder="var1 + var2 * 0.5", key="cf_expr")
            
            col_cf1, col_cf2 = st.columns([3, 1])
            with col_cf1:
                if st.button("Add", key="add_cf"):
                    if custom_name and custom_expr:
                        st.session_state.custom_formulas.append({
                            'formula_name': custom_name,
                            'formula_expression': custom_expr
                        })
                        st.success(f"✅ Added: {custom_name}")
                        st.rerun()
            
            if st.session_state.custom_formulas:
                st.caption("**Custom Formulas:**")
                for idx, cf in enumerate(st.session_state.custom_formulas):
                    col_a, col_b = st.columns([5, 1])
                    with col_a:
                        st.caption(f"`{cf['formula_name']}`")
                    with col_b:
                        if st.button("🗑️", key=f"del_cf_{idx}"):
                            st.session_state.custom_formulas.pop(idx)
                            st.rerun()
                
    
    # Variable Mapping Section
    if st.session_state.initial_mapping_done and st.session_state.variable_mappings:
        st.markdown("---")
        st.subheader("🔗 Variable to Header Mappings")
        st.markdown("Review and edit the automatically generated mappings. You can manually adjust any mapping.")
        
        # Create tabs for different views
        tab1, tab2 = st.tabs(["📊 Mappings Table", "📋 All Excel Headers"])
        
        with tab1:
            st.markdown("#### Variable Mappings")
            
            # Header row
            # Replace the header row columns with:
            col_h1, col_h2, col_h3, col_h4, col_h5, col_h6 = st.columns([2, 3, 1.5, 1.5, 1, 1])
            with col_h1:
                st.markdown("**Variable**")
            with col_h2:
                st.markdown("**Mapped Header**")
            with col_h3:
                st.markdown("**Confidence**")
            with col_h4:
                st.markdown("**Method**")
            with col_h5:
                st.markdown("**Verified**")
            with col_h6:
                st.markdown("**AI Assist**")
                        
            st.markdown('<hr style="margin: 0.5rem 0; border: 0; border-top: 2px solid #004DA8;">', unsafe_allow_html=True)
            
            # In the tab1 section, replace the sorted_vars logic with:
            # Separate variables and unmapped headers
            actual_vars = [v for v in st.session_state.variable_mappings.keys() 
                        if not v.startswith('_header_')]
            unmapped_headers = [v for v in st.session_state.variable_mappings.keys() 
                            if v.startswith('_header_')]

            # Sort: unmapped vars first, then by confidence, then unmapped headers
            sorted_vars = sorted(
                actual_vars,
                key=lambda v: (
                    1 if st.session_state.variable_mappings[v].mapped_header else 0,
                    -st.session_state.variable_mappings[v].confidence_score
                )
            ) + sorted(unmapped_headers)
            
            for var_name in sorted_vars:
                mapping = st.session_state.variable_mappings[var_name]
                
                # In the mapping table loop, replace the columns definition with:
                col1, col2, col3, col4, col5, col6 = st.columns([2, 3, 1.5, 1.5, 1, 1])

                with col1:
                    st.markdown(f"`{var_name}`")
                
                with col2:
                    current_index = 0
                    dropdown_options = ["(None of the following)"] + st.session_state.excel_headers
                    
                    if mapping.mapped_header in st.session_state.excel_headers:
                        current_index = st.session_state.excel_headers.index(mapping.mapped_header) + 1
                    
                    new_header = st.selectbox(
                        "Header",
                        options=dropdown_options,
                        index=current_index,
                        key=f"header_{var_name}",
                        label_visibility="collapsed"
                    )
                    
                    # Update mapping if changed
                    # Treat "(None of the above)" as empty
                    if new_header == "(None of the above)":
                        new_header = ""
                    
                    if new_header != mapping.mapped_header:
                        mapping.mapped_header = new_header
                        mapping.confidence_score = 1.0 if new_header else 0.0
                        mapping.matching_method = "manual" if new_header else "none_selected"
                        mapping.is_verified = True if new_header else False
                
                with col3:
                    # Confidence badge
                    if mapping.confidence_score >= CONFIDENCE_THRESHOLDS['high']:
                        conf_class = "confidence-high"
                        conf_label = "High"
                    elif mapping.confidence_score >= CONFIDENCE_THRESHOLDS['medium']:
                        conf_class = "confidence-medium"
                        conf_label = "Medium"
                    elif mapping.confidence_score >= CONFIDENCE_THRESHOLDS['low']:
                        conf_class = "confidence-low"
                        conf_label = "Low"
                    else:
                        conf_class = "confidence-low"
                        conf_label = "None"
                    
                    st.markdown(
                        f'<span class="{conf_class}">{conf_label} ({mapping.confidence_score:.2f})</span>',
                        unsafe_allow_html=True
                    )
                
                with col4:
                    st.markdown(f"*{mapping.matching_method[:20]}...*" if len(mapping.matching_method) > 20 else f"*{mapping.matching_method}*")
                
                with col5:
                    verified = st.checkbox(
                        "Verified",
                        value=mapping.is_verified,
                        key=f"verify_{var_name}",
                        label_visibility="collapsed"
                    )
                    mapping.is_verified = verified
                with col6:
                    is_header = var_name.startswith('_header_')
                    if not is_header:
                        ai_selected = st.checkbox(
                            "AI",
                            value=var_name in st.session_state.get('ai_assist_vars', []),
                            key=f"ai_{var_name}",
                            label_visibility="collapsed",
                            help="Select for AI assist"
                        )
                        if ai_selected and var_name not in st.session_state.get('ai_assist_vars', []):
                            if 'ai_assist_vars' not in st.session_state:
                                st.session_state.ai_assist_vars = []
                            st.session_state.ai_assist_vars.append(var_name)
                        elif not ai_selected and var_name in st.session_state.get('ai_assist_vars', []):
                            st.session_state.ai_assist_vars.remove(var_name)
                                
                st.markdown('<hr style="margin: 0.5rem 0; border: 0; border-top: 1px solid #e0e0e0;">', unsafe_allow_html=True)
        
        with tab2:
            st.markdown("#### All Excel Headers (Mapped & Unmapped)")
            
            # Get mapped headers
            mapped_headers = {m.mapped_header for m in st.session_state.variable_mappings.values() if m.mapped_header}
            
            # Show all headers with status
            headers_df_data = []
            for header in st.session_state.excel_headers:
                if header in mapped_headers:
                    # Find which variable(s) map to this header
                    vars_mapped = [v for v, m in st.session_state.variable_mappings.items() if m.mapped_header == header]
                    status = f"Mapped to: {', '.join(vars_mapped)}"
                    status_class = "✅"
                else:
                    status = "Not mapped to any variable"
                    status_class = "⚪"
                
                headers_df_data.append({
                    "Status": status_class,
                    "Header Name": header,
                    "Mapping Info": status
                })
            
            headers_df = pd.DataFrame(headers_df_data)
            st.dataframe(headers_df, use_container_width=True, hide_index=True)
            
            unmapped_count = len([h for h in st.session_state.excel_headers if h not in mapped_headers])
            st.info(f"📊 **{unmapped_count}** out of **{len(st.session_state.excel_headers)}** headers are currently unmapped")
        
        # Summary statistics
        st.markdown("---")
        col_stat1, col_stat2, col_stat3, col_stat4 = st.columns(4)
        
        total_vars = len(st.session_state.variable_mappings)
        mapped_vars = len([m for m in st.session_state.variable_mappings.values() if m.mapped_header])
        verified_vars = len([m for m in st.session_state.variable_mappings.values() if m.is_verified])
        high_conf_vars = len([m for m in st.session_state.variable_mappings.values() if m.confidence_score >= CONFIDENCE_THRESHOLDS['high']])
        
        with col_stat1:
            st.metric("Total Variables", total_vars)
        with col_stat2:
            st.metric("Mapped", mapped_vars)
        with col_stat3:
            st.metric("Verified", verified_vars)
        with col_stat4:
            st.metric("High Confidence", high_conf_vars)
        
        # Replace the entire "AI Enhancement Section" with:
        st.markdown("---")
        st.subheader("🤖 AI-Powered Enhancement")

        if 'ai_assist_vars' not in st.session_state:
            st.session_state.ai_assist_vars = []

        # Multiselect for choosing variables for AI assist
        all_var_names = sorted([v for v in st.session_state.variable_mappings.keys() 
                                if not v.startswith('_header_')])
        selected_for_ai = st.multiselect(
            "Select variables for AI semantic matching:",
            options=all_var_names,
            default=st.session_state.ai_assist_vars,
            help="Choose variables where you want AI to find better matches"
        )
        st.session_state.ai_assist_vars = selected_for_ai

        if selected_for_ai:
            if st.button("🤖 Run AI Assist for Selected Variables", type="secondary", disabled=MOCK_MODE):
                with st.spinner(f"Running AI semantic matching on {len(selected_for_ai)} variables..."):
                    matcher = VariableHeaderMatcher()
                    improved_count = 0
                    for var in selected_for_ai:
                        if var in st.session_state.variable_mappings:
                            improved_mapping = matcher.find_best_match(
                                var, 
                                st.session_state.excel_headers, 
                                use_ai=True
                            )
                            if improved_mapping and improved_mapping.confidence_score > \
                            st.session_state.variable_mappings[var].confidence_score:
                                st.session_state.variable_mappings[var] = improved_mapping
                                improved_count += 1
                    
                    st.success(f"✅ AI improved {improved_count} out of {len(selected_for_ai)} mappings!")
                    st.session_state.ai_assist_vars = []
                    st.rerun()
        else:
            st.info("💡 Select variables above and click the AI assist button to improve their mappings")
        
        # Proceed button
        st.markdown("---")
        col_btn1, col_btn2 = st.columns([1, 1])
        
        with col_btn1:
            if st.button("✅ Confirm Mappings & View Formulas", type="primary"):
                # Check if all variables are mapped
                unmapped = [v for v, m in st.session_state.variable_mappings.items() if not m.mapped_header]
                
                if unmapped:
                    st.warning(f"⚠️ {len(unmapped)} variables are still unmapped: {', '.join(unmapped[:5])}{'...' if len(unmapped) > 5 else ''}")
                    st.info("You can proceed anyway, but unmapped variables won't be replaced in formulas.")
                
                st.session_state.mapping_complete = True
                st.success("✅ Mappings confirmed!")
                st.rerun()
        
        with col_btn2:
            # Export mappings
            mapping_export = {
                var: {
                    'mapped_header': m.mapped_header,
                    'confidence': m.confidence_score,
                    'method': m.matching_method,
                    'verified': m.is_verified
                }
                for var, m in st.session_state.variable_mappings.items()
            }
            
            st.download_button(
                label="📥 Export Mappings",
                data=json.dumps(mapping_export, indent=2),
                file_name="variable_mappings.json",
                mime="application/json"
            )
    
    # Show mapped formulas
    if st.session_state.mapping_complete:
        st.markdown("---")
        st.subheader("📐 Formulas with Mapped Headers")
        st.markdown("Here are your formulas with variables replaced by the mapped Excel headers (shown in brackets).")
        
        mapped_formulas = apply_mappings_to_formulas(
            st.session_state.formulas,
            st.session_state.variable_mappings
        )
        
        for formula in mapped_formulas:
            with st.expander(f"**{formula['formula_name']}**", expanded=True):
                col_f1, col_f2 = st.columns(2)
                
                with col_f1:
                    st.markdown("**Original Expression:**")
                    st.code(formula['original_expression'], language="python")
                
                with col_f2:
                    st.markdown("**Mapped Expression:**")
                    st.code(formula['mapped_expression'], language="python")
        
        # Export mapped formulas
        st.markdown("---")
        col_exp1, col_exp2, col_exp3 = st.columns([1, 1, 1])
        
        with col_exp1:
            st.download_button(
                label="📥 Download Mapped Formulas (JSON)",
                data=json.dumps(mapped_formulas, indent=2),
                file_name="mapped_formulas.json",
                mime="application/json"
            )
        
        with col_exp2:
            # CSV export
            csv_data = pd.DataFrame(mapped_formulas).to_csv(index=False)
            st.download_button(
                label="📥 Download Mapped Formulas (CSV)",
                data=csv_data,
                file_name="mapped_formulas.csv",
                mime="text/csv"
            )
        
        with col_exp3:
            # After the "Confirm Mappings & View Formulas" section, add:
            if st.session_state.mapping_complete:
                st.markdown("---")
                if st.button("➡️ Proceed to Calculations", type="primary", key="goto_calc"):
                    # Import and run calculation engine
                    try:
                        import calculation_engine
                        calculation_engine.main()
                    except ImportError:
                        st.error("❌ calculation_engine.py not found. Please ensure it's in the same directory.")
                    except Exception as e:
                        st.error(f"❌ Error loading calculation engine: {e}")
    
    # Footer
    st.markdown("---")
    st.markdown(
        """
        <div style="text-align: center; margin-top: 50px; color: #7f8c8d; font-size: 0.9em;">
            <p>Variable Mapping Module | Developed with Streamlit and AI</p>
        </div>
        """,
        unsafe_allow_html=True
    )


if __name__ == "__main__":
    main()