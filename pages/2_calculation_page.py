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
        """Match all variables to headers"""
        mappings = {}
        
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        total_vars = len(variables)
        
        for idx, var in enumerate(variables):
            # Update progress
            progress = (idx + 1) / total_vars
            progress_bar.progress(progress)
            status_text.text(f"Matching variable {idx + 1}/{total_vars}: {var}")
            
            mapping = self.find_best_match(var, headers, use_ai=use_ai)
            if mapping:
                mappings[var] = mapping
            else:
                # Create unmapped entry
                mappings[var] = VariableMapping(
                    variable_name=var,
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
        
        # Add custom formula section
        st.markdown("---")
        st.subheader("➕ Add Custom Formula for Mapping")
        
        with st.form("custom_formula_form"):
            custom_name = st.text_input("Formula Name", placeholder="e.g., Custom_Calculation")
            custom_expr = st.text_area("Formula Expression", placeholder="e.g., variable1 + variable2 * 0.5")
            
            submitted = st.form_submit_button("Add Custom Formula")
            if submitted and custom_name and custom_expr:
                st.session_state.custom_formulas.append({
                    'formula_name': custom_name,
                    'formula_expression': custom_expr
                })
                st.success(f"✅ Added custom formula: {custom_name}")
                st.rerun()
        
        if st.session_state.custom_formulas:
            st.markdown("**Custom Formulas:**")
            for idx, cf in enumerate(st.session_state.custom_formulas):
                col_cf1, col_cf2 = st.columns([4, 1])
                with col_cf1:
                    st.code(f"{cf['formula_name']} = {cf['formula_expression']}", language="python")
                with col_cf2:
                    if st.button("🗑️", key=f"delete_cf_{idx}"):
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
            col_h1, col_h2, col_h3, col_h4, col_h5 = st.columns([2, 3, 2, 2, 2])
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
            
            st.markdown('<hr style="margin: 0.5rem 0; border: 0; border-top: 2px solid #004DA8;">', unsafe_allow_html=True)
            
            # Sort mappings: unmapped first, then by confidence
            sorted_vars = sorted(
                st.session_state.variable_mappings.keys(),
                key=lambda v: (
                    1 if st.session_state.variable_mappings[v].mapped_header else 0,
                    -st.session_state.variable_mappings[v].confidence_score
                )
            )
            
            for var_name in sorted_vars:
                mapping = st.session_state.variable_mappings[var_name]
                
                col1, col2, col3, col4, col5 = st.columns([2, 3, 2, 2, 2])
                
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
        
        # AI Enhancement Section
        st.markdown("---")
        st.subheader("🤖 AI-Powered Enhancement (Optional)")
        
        # Get variables that selected "None of the above"
        none_selected_vars = [v for v, m in st.session_state.variable_mappings.items() 
                             if not m.mapped_header and m.matching_method == "none_selected"]
        unmapped_vars = [v for v, m in st.session_state.variable_mappings.items() 
                        if not m.mapped_header and m.matching_method != "none_selected"]
        low_conf_vars = [v for v, m in st.session_state.variable_mappings.items() 
                        if m.mapped_header and m.confidence_score < CONFIDENCE_THRESHOLDS['medium']]
        
        if none_selected_vars:
            st.info(f"ℹ️ {len(none_selected_vars)} variable(s) marked as 'None of the above' - AI will attempt to find mappings for these")
        
        if unmapped_vars or low_conf_vars or none_selected_vars:
            if unmapped_vars or low_conf_vars:
                st.warning(f"⚠️ Found {len(unmapped_vars)} unmapped and {len(low_conf_vars)} low-confidence mappings")
            
            st.markdown("**Use AI to improve these mappings:**")
            
            col_ai1, col_ai2, col_ai3 = st.columns([1, 1, 1])
            
            with col_ai1:
                if st.button("🤖 AI for Unmapped", type="secondary", disabled=MOCK_MODE or not unmapped_vars):
                    with st.spinner("Running AI semantic matching on unmapped variables..."):
                        matcher = VariableHeaderMatcher()
                        for var in unmapped_vars:
                            improved_mapping = matcher.find_best_match(var, st.session_state.excel_headers, use_ai=True)
                            if improved_mapping and improved_mapping.confidence_score > st.session_state.variable_mappings[var].confidence_score:
                                st.session_state.variable_mappings[var] = improved_mapping
                        
                        st.success("✅ AI matching complete!")
                        st.rerun()
            
            with col_ai2:
                if st.button("🤖 AI for Low Confidence", type="secondary", disabled=MOCK_MODE or not low_conf_vars):
                    with st.spinner("Running AI semantic matching on low confidence mappings..."):
                        matcher = VariableHeaderMatcher()
                        for var in low_conf_vars:
                            improved_mapping = matcher.find_best_match(var, st.session_state.excel_headers, use_ai=True)
                            if improved_mapping and improved_mapping.confidence_score > st.session_state.variable_mappings[var].confidence_score:
                                st.session_state.variable_mappings[var] = improved_mapping
                        
                        st.success("✅ AI matching complete!")
                        st.rerun()
            
            with col_ai3:
                if st.button("🤖 AI for 'None of above'", type="secondary", disabled=MOCK_MODE or not none_selected_vars):
                    with st.spinner("Running AI semantic matching on 'None of the above' variables..."):
                        matcher = VariableHeaderMatcher()
                        for var in none_selected_vars:
                            improved_mapping = matcher.find_best_match(var, st.session_state.excel_headers, use_ai=True)
                            if improved_mapping and improved_mapping.confidence_score > st.session_state.variable_mappings[var].confidence_score:
                                st.session_state.variable_mappings[var] = improved_mapping
                        
                        st.success("✅ AI matching complete!")
                        st.rerun()
        else:
            st.success("✅ All variables are mapped with good confidence!")
        
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
            if st.button("➡️ Proceed to Calculations", type="primary"):
                st.session_state.mapping_complete = True
                st.rerun()
    
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