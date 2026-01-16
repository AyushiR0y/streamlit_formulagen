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
    
    def find_best_match(self, target: str, candidates: List[str], use_ai: bool = False) -> Optional[VariableMapping]:
        """
        Generic finder.
        Finds the best matching candidate for the target.
        Used as: find_best_match(Excel_Header, Variable_List)
        """
        best_score = 0.0
        best_candidate = None
        best_method = "no_match"
        
        # Stage 1: Lexical matching
        for candidate in candidates:
            lex_score = self.lexical_similarity(target, candidate)
            
            if lex_score > best_score:
                best_score = lex_score
                best_candidate = candidate
                best_method = "lexical"
        
        # Stage 2: Fuzzy matching
        for candidate in candidates:
            fuzzy_score = self.fuzzy_similarity(target, candidate)
            
            if fuzzy_score > best_score:
                best_score = fuzzy_score
                best_candidate = candidate
                best_method = "fuzzy"
        
        # Stage 3: AI semantic matching (only if enabled)
        if use_ai and best_candidate:
            ai_score, ai_method = self.semantic_similarity_ai(target, best_candidate)
            
            if ai_score > best_score:
                best_score = ai_score
                best_method = ai_method
        
        if best_candidate and best_score >= CONFIDENCE_THRESHOLDS['low']:
            return VariableMapping(
                variable_name=target, # In this context, the variable_name is the Header we are mapping
                mapped_header=best_candidate, # The mapped_header is the Variable found
                confidence_score=best_score,
                matching_method=best_method,
                is_verified=best_score >= CONFIDENCE_THRESHOLDS['high']
            )
        
        return None
    
    def match_all(self, targets: List[str], candidates: List[str], use_ai: bool = False) -> Dict[str, VariableMapping]:
        """
        Maps all targets to best candidates.
        Structure of returned dict: {target: MappingObject}
        """
        mappings = {}
        
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        total_targets = len(targets)
        
        for idx, target in enumerate(targets):
            progress = (idx + 1) / total_targets
            progress_bar.progress(progress)
            status_text.text(f"Mapping {target}...")
            
            mapping = self.find_best_match(target, candidates, use_ai=use_ai)
            if mapping:
                mappings[target] = mapping
            else:
                # If no good match found, create an empty mapping
                mappings[target] = VariableMapping(
                    variable_name=target,
                    mapped_header="",
                    confidence_score=0.0,
                    matching_method="no_match",
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

def apply_mappings_to_formulas(formulas: List[Dict], header_to_var_mapping: Dict[str, str]) -> List[Dict]:
    """
    Replace variables in formulas with mapped Excel Headers.
    New Logic: 
    header_to_var_mapping format: { "Excel_Header": "VariableName" }
    Formula: "VariableName * 2"
    Result: "[Excel_Header] * 2"
    """
    mapped_formulas = []
    
    for formula in formulas:
        expr = formula.get('formula_expression', '')
        
        # Replace each variable with its mapped header
        # We iterate over the mapping dict items (Header, Var)
        for excel_header, var_name in header_to_var_mapping.items():
            if excel_header and var_name:
                # Use word boundaries to avoid partial replacements
                pattern = r'\b' + re.escape(var_name) + r'\b'
                expr = re.sub(pattern, f'[{excel_header}]', expr)
        
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
                    # Placeholder for calculation logic
                    result_df = calc_df.copy()
                    calc_results = []
                    
                    # Store results
                    st.session_state.results_df = result_df
                    st.session_state.calc_results = calc_results
                    st.session_state.calculation_complete = True
                    
                    st.success("✅ Calculations complete!")
                    st.rerun()


def get_all_master_variables():
    """Aggregates variables from Input, Formula, and Derived sources"""
    all_vars = set()
    
    # 1. Static Input Variables
    all_vars.update(INPUT_VARIABLES.keys())
    
    # 2. Extracted from Formulas
    if 'formulas' in st.session_state and st.session_state.formulas:
        formula_vars, derived_defs = extract_variables_from_formulas(st.session_state.formulas)
        all_vars.update(formula_vars)
        all_vars.update(derived_defs.keys())
    
    # 3. Custom Formulas
    if 'custom_formulas' in st.session_state and st.session_state.custom_formulas:
        for cf in st.session_state.custom_formulas:
            cf_vars, cf_derived = extract_variables_from_formulas([cf])
            all_vars.update(cf_vars)
            all_vars.update(cf_derived.keys())
            
    return sorted(list(all_vars))

def main():
    load_css()
    
    st.set_page_config(
        page_title="Variable Mapping",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    
    
    
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
    if 'formulas' not in st.session_state:
        st.session_state.formulas = []

    if not st.session_state.formulas:
        st.error("❌ No formulas found in session.")
        st.warning("⚠️ **Note:** Refreshing this page will clear your session. Use the navigation menu instead of browser refresh.")
        st.info("💡 Use the sidebar navigation to return to the main page and extract formulas again.")
        
        # Option to upload previously saved mappings
        st.markdown("---")
        st.subheader("📥 Restore Previous Session")
        
        col_restore1, col_restore2 = st.columns(2)
        
        with col_restore1:
            st.markdown("**Upload Formulas JSON:**")
            uploaded_json = st.file_uploader("Upload previously exported mappings JSON", type=['json'], key="restore_json")
            if uploaded_json:
                try:
                    import_data = json.loads(uploaded_json.read())
                    if 'formulas' in import_data:
                        st.session_state.formulas = import_data['formulas']
                        st.success("✅ Formulas restored!")
                        st.rerun()
                    else:
                        st.error("❌ Invalid JSON format. Missing 'formulas' key.")
                except Exception as e:
                    st.error(f"Error loading file: {e}")
        
        with col_restore2:
            st.markdown("**Or go back to extract formulas:**")
            st.info("Use the sidebar navigation to return to the main page.")
        
        return
    
    if 'excel_headers' not in st.session_state:
        st.session_state.excel_headers = []
    
    if 'header_to_var_mapping' not in st.session_state:
        st.session_state.header_to_var_mapping = {}
        
    if 'removed_headers' not in st.session_state:
        st.session_state.removed_headers = []
    
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
        
    # State for user's variable selection
    if 'selected_variables_for_mapping' not in st.session_state:
        st.session_state.selected_variables_for_mapping = []
    
   # Check if we're in calculation results mode
    if st.session_state.mapping_complete and st.session_state.calculation_complete:
        show_calculation_results()
        return

    # Check if we're ready for calculations
    if st.session_state.mapping_complete:
        show_calculation_engine()
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
                    
                    # --- Filter Variables Step ---
                    st.markdown("---")
                    st.subheader("🛠️ Filter Variables for Mapping")
                    st.markdown("Deselect variables that are **not** relevant to this specific Excel file.")
                    
                    all_master_vars = get_all_master_variables()
                    
                    # Default to selecting all variables if it's a fresh file upload
                    if st.session_state.selected_variables_for_mapping == [] or \
                       set(st.session_state.excel_headers) != set(st.session_state.get('last_uploaded_headers', [])):
                        st.session_state.selected_variables_for_mapping = all_master_vars
                        st.session_state.last_uploaded_headers = st.session_state.excel_headers
                    
                    st.session_state.selected_variables_for_mapping = st.multiselect(
                        "Select Variables to Map",
                        options=all_master_vars,
                        default=st.session_state.selected_variables_for_mapping,
                        key="variable_filter_multiselect",
                        help="Variables NOT selected here will be ignored by the automatic mapper."
                    )
                    
                    # Button directly below
                    st.markdown('<br>', unsafe_allow_html=True)
                    if st.button("🔗 Start Automatic Mapping", type="primary", key="start_mapping_btn", disabled=st.session_state.initial_mapping_done):
                        with st.spinner("Analyzing headers and matching with variables..."):
                            active_variables = st.session_state.selected_variables_for_mapping
                            
                            matcher = VariableHeaderMatcher()
                            
                            # Map Headers -> Variables
                            mappings = matcher.match_all(
                                targets=headers,
                                candidates=active_variables,
                                use_ai=False
                            )
                            
                            # Update session state with Header -> Variable mapping
                            new_mapping = {}
                            for header, mapping_obj in mappings.items():
                                new_mapping[header] = mapping_obj.mapped_header
                            
                            st.session_state.header_to_var_mapping = new_mapping
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
                            
                            st.success(f"✅ Mapped {mapped} out of {total} headers using {len(active_variables)} variables")
                            
                            if method_counts:
                                st.markdown("**Matching Methods Used:**")
                                method_df = pd.DataFrame([
                                    {"Method": method.title(), "Count": count}
                                    for method, count in sorted(method_counts.items(), key=lambda x: -x[1])
                                ])
                                st.dataframe(method_df, hide_index=True, use_container_width=True)
                            
                            st.rerun()
                    # --------------------------------------
    
    with col2:
        st.subheader("📋 Available Variables")
        st.markdown("Variables available for mapping: **Input**, **Derived**, and **Extracted** from formulas.")
        
        # Get consolidated variable list
        all_variables = get_all_master_variables()
        
        if all_variables:
            # Categorize variables for display
            input_vars = set(INPUT_VARIABLES.keys())
            formula_vars, derived_defs = extract_variables_from_formulas(st.session_state.formulas)
            
            var_df_data = []
            for var in sorted(all_variables):
                v_type = "Input"
                if var in derived_defs:
                    v_type = "Derived"
                elif var not in input_vars:
                    v_type = "Extracted"
                var_df_data.append({'Variable Name': var, 'Type': v_type})
            
            var_df = pd.DataFrame(var_df_data)
            st.dataframe(var_df, use_container_width=True, hide_index=True)
            
            # Show derived variable formulas
            if derived_defs:
                with st.expander("📐 Derived Variable Definitions", expanded=False):
                    for var, formula in sorted(derived_defs.items()):
                        st.markdown(f"**`{var}`** = `{formula}`")
        else:
            st.info("No variables detected.")
       
        st.subheader("➕ Add Custom Formula")
        
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
        # ------------------------------------------------
                
    
    # Variable Mapping Section
    if st.session_state.initial_mapping_done:
        st.markdown("---")
        st.subheader("🔗 Header to Variable Mappings")
        st.markdown("Review and edit the mappings. Rows represent **Excel Headers**. Map them to the **Variables** used in formulas.")
        
        # Create tabs for different views
        tab1, tab2 = st.tabs(["📊 Mappings Table", "📋 JSON View"])
        
        with tab1:
            st.markdown("#### Mapping Configuration")
            
            # Header row
            col_h1, col_h2, col_h3 = st.columns([3, 3, 1])
            with col_h1:
                st.markdown("**Excel Header**")
            with col_h2:
                st.markdown("**Mapped Variable**")
            with col_h3:
                st.markdown("**Actions**")
                        
            st.markdown('<hr style="margin: 0.5rem 0; border: 0; border-top: 2px solid #004DA8;">', unsafe_allow_html=True)
            
            # Get current variables for dropdown
            current_variables = get_all_master_variables()
            
            # Filter out removed headers
            active_headers = [h for h in st.session_state.excel_headers if h not in st.session_state.removed_headers]
            
            for header in active_headers:
                # Get current mapping for this header
                current_var = st.session_state.header_to_var_mapping.get(header, "")
                
                col1, col2, col3 = st.columns([3, 3, 1])

                with col1:
                    st.text_input("Header", value=header, key=f"h_txt_{header}", label_visibility="collapsed", disabled=True)
                
                with col2:
                    # Dropdown options: (None) + all variables
                    dropdown_options = ["(None of the following)"] + current_variables
                    
                    # Determine index safely
                    try:
                        idx = dropdown_options.index(current_var)
                    except ValueError:
                        idx = 0
                    
                    new_var = st.selectbox(
                        "Variable",
                        options=dropdown_options,
                        index=idx,
                        key=f"var_select_{header}",
                        label_visibility="collapsed"
                    )
                    
                    # Update mapping if changed
                    final_var = "" if new_var == "(None of the following)" else new_var
                    
                    if final_var != current_var:
                        st.session_state.header_to_var_mapping[header] = final_var
                
                with col3:
                    # Remove Button
                    if st.button("🗑️", key=f"remove_{header}", help="Remove this column from calculations"):
                        st.session_state.removed_headers.append(header)
                        # Also remove from mapping to clean up
                        if header in st.session_state.header_to_var_mapping:
                            del st.session_state.header_to_var_mapping[header]
                        st.rerun()
                                
                st.markdown('<hr style="margin: 0.5rem 0; border: 0; border-top: 1px solid #e0e0e0;">', unsafe_allow_html=True)
        
        with tab2:
            st.markdown("#### Current Mappings (JSON)")
            
            # Filter out empty mappings
            active_mapping = {
                h: v for h, v in st.session_state.header_to_var_mapping.items() 
                if v and h not in st.session_state.removed_headers
            }
            
            st.json(active_mapping)
            
            # Provide download for this JSON
            st.download_button(
                label="📥 Download Mapping JSON",
                data=json.dumps(active_mapping, indent=2),
                file_name="header_to_variable_mapping.json",
                mime="application/json"
            )
            
            # Show removed headers
            if st.session_state.removed_headers:
                st.markdown("**Removed Headers:**")
                st.write(st.session_state.removed_headers)
        
        # Summary statistics
        st.markdown("---")
        col_stat1, col_stat2, col_stat3 = st.columns(3)
        
        total_headers = len(st.session_state.excel_headers)
        active_count = len([h for h in st.session_state.excel_headers if h not in st.session_state.removed_headers])
        mapped_count = len([v for v in st.session_state.header_to_var_mapping.values() if v])
        
        with col_stat1:
            st.metric("Total Headers", total_headers)
        with col_stat2:
            st.metric("Active Columns", active_count)
        with col_stat3:
            st.metric("Mapped Variables", mapped_count)
        
        # AI Enhancement Section
        st.markdown("---")
        st.subheader("🤖 AI-Powered Enhancement")

        if 'ai_assist_headers' not in st.session_state:
            st.session_state.ai_assist_headers = []

        # Multiselect for choosing headers for AI assist
        active_headers_list = [h for h in st.session_state.excel_headers if h not in st.session_state.removed_headers]
        selected_for_ai = st.multiselect(
            "Select headers for AI semantic matching:",
            options=active_headers_list,
            default=st.session_state.ai_assist_headers,
            help="Choose headers where you want AI to find better variable matches"
        )
        st.session_state.ai_assist_headers = selected_for_ai

        if selected_for_ai:
            if st.button("🤖 Run AI Assist for Selected Headers", type="secondary", disabled=MOCK_MODE):
                with st.spinner(f"Running AI semantic matching on {len(selected_for_ai)} headers..."):
                    matcher = VariableHeaderMatcher()
                    current_variables = st.session_state.selected_variables_for_mapping if st.session_state.selected_variables_for_mapping else get_all_master_variables()
                    
                    improved_count = 0
                    for header in selected_for_ai:
                        improved_mapping = matcher.find_best_match(
                            header, 
                            current_variables, 
                            use_ai=True
                        )
                        if improved_mapping and improved_mapping.mapped_header:
                            current_val = st.session_state.header_to_var_mapping.get(header)
                            if current_val != improved_mapping.mapped_header:
                                st.session_state.header_to_var_mapping[header] = improved_mapping.mapped_header
                                improved_count += 1
                    
                    st.success(f"✅ AI updated {improved_count} mappings!")
                    st.session_state.ai_assist_headers = []
                    st.rerun()
        else:
            st.info("💡 Select headers above and click the AI assist button to improve their mappings")
        
        # Proceed button
        st.markdown("---")
        col_btn1, col_btn2 = st.columns([1, 1])
        
        with col_btn1:
            if st.button("✅ Confirm Mappings & View Formulas", type="primary"):
                # Check for unmapped active headers
                unmapped = [h for h in active_headers if not st.session_state.header_to_var_mapping.get(h)]
                
                if unmapped:
                    st.warning(f"⚠️ {len(unmapped)} headers are unmapped: {', '.join(unmapped[:5])}{'...' if len(unmapped) > 5 else ''}")
                
                st.session_state.mapping_complete = True
                st.success("✅ Mappings confirmed!")
                st.rerun()
        
        with col_btn2:
             # Export current view mapping
            active_mapping = {
                h: v for h, v in st.session_state.header_to_var_mapping.items() 
                if v and h not in st.session_state.removed_headers
            }
            st.download_button(
                label="📥 Export Final Mappings",
                data=json.dumps(active_mapping, indent=2),
                file_name="final_mappings.json",
                mime="application/json"
            )
    
    # Show mapped formulas
    if st.session_state.mapping_complete:
        st.markdown("---")
        st.subheader("📐 Formulas with Mapped Headers")
        st.markdown("Variables in formulas have been replaced by the mapped Excel headers (shown in brackets).")
        
        # Pass the header->var mapping to the apply function
        mapped_formulas = apply_mappings_to_formulas(
            st.session_state.formulas,
            st.session_state.header_to_var_mapping
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
            if st.button("➡️ Proceed to Calculations", type="primary", key="goto_calc"):
                try:
                    # Logic to switch to calculation view
                    st.session_state.calculation_view = True
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ Error: {e}")
    
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