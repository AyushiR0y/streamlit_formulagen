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
    variable_type: str = "output"  # Add this line
    
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
    
    def semantic_similarity_ai_batch(self, header: str, all_variables: List[str]) -> Tuple[str, float, str]:
        """Find best matching variable for a header using AI in one call
        
        Args:
            header: The Excel header to match
            all_variables: List of all variable names (output/input/derived)
            
        Returns:
            (best_variable, confidence_score, method_description)
        """
        if MOCK_MODE or not client:
            return "", 0.0, "AI unavailable"
        
        try:
            # Create a concise list of variables
            var_list = "\n".join([f"- {v}" for v in all_variables[:50]])  # Limit to 50 to save tokens
            
            prompt = f"""You are matching an Excel column header to variable names from insurance policy formulas.

Excel Header: "{header}"

Available Variables:
{var_list}

Task: Find the BEST matching variable for this header. Consider:
- Semantic similarity (same concept, synonyms)
- Insurance/financial domain context
- Abbreviations (e.g., 'SA' = 'Sum Assured', 'FUP' = 'First Unpaid Premium')

Response format (one line only):
VARIABLE: variable_name | SCORE: 0.XX

If no good match exists, respond with: VARIABLE: none | SCORE: 0.00"""

            response = client.chat.completions.create(
                model=DEPLOYMENT_NAME,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=50,  # Reduced from 150
                temperature=0.0  # More deterministic
            )
            
            response_text = response.choices[0].message.content.strip()
            
            # Parse response
            var_match = re.search(r'VARIABLE:\s*([^\|]+)', response_text, re.IGNORECASE)
            score_match = re.search(r'SCORE:\s*([0-9]*\.?[0-9]+)', response_text, re.IGNORECASE)
            
            if var_match and score_match:
                variable = var_match.group(1).strip()
                score = float(score_match.group(1))
                
                if variable.lower() == "none":
                    return "", 0.0, "AI: no match"
                
                return variable, min(score, 1.0), "AI_batch"
            
            return "", 0.0, "AI: parse error"
            
        except Exception as e:
            return "", 0.0, f"AI_error: {str(e)}"
    
    def find_best_match(self, variable: str, headers: List[str], all_variables: Dict[str, str] = None, use_ai: bool = False) -> Optional[VariableMapping]:
        """Find the best matching header for a variable
        
        Args:
            variable: The variable to match
            headers: List of Excel headers to match against
            all_variables: Dict of {variable_name: variable_type} for context (optional)
            use_ai: Whether to use AI semantic matching
        """
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
        
        # Stage 3: AI semantic matching (only if enabled and we found a candidate)
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
    
    def match_all_variables(self, variables: List[str], headers: List[str], all_variables: Dict[str, str] = None, use_ai: bool = False) -> Dict[str, VariableMapping]:
        """Match all variables to headers
        
        Args:
            variables: List of variable names to match
            headers: List of Excel headers
            all_variables: Dict of {variable_name: variable_type} for all vars (output/input/derived)
            use_ai: Whether to use AI
        """
        mappings = {}
        
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        total_vars = len(variables)
        
        for idx, var in enumerate(variables):
            # Update progress
            progress = (idx + 1) / total_vars
            progress_bar.progress(progress)
            status_text.text(f"Matching variable {idx + 1}/{total_vars}: {var}")
            
            mapping = self.find_best_match(var, headers, all_variables=all_variables, use_ai=use_ai)
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

def ai_match_single_header(self, header: str, all_variables_dict: Dict[str, str]) -> Tuple[str, str, float]:
        """Match a single header to best variable using AI
        
        Args:
            header: Excel header to match
            all_variables_dict: Dict of {var_name: var_type} e.g. {"TERM_START_DATE": "input"}
            
        Returns:
            (variable_name, variable_type, confidence_score)
        """
        all_var_names = list(all_variables_dict.keys())
        best_var, score, method = self.semantic_similarity_ai_batch(header, all_var_names)
        
        if best_var and best_var in all_variables_dict:
            var_type = all_variables_dict[best_var]
            return best_var, var_type, score
        
        return "", "", 0.0
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
    # Cache formulas in browser session storage to prevent loss on refresh
    if 'formulas' not in st.session_state:
        st.session_state.formulas = []
    
    if 'custom_formulas' not in st.session_state:
        st.session_state.custom_formulas = []
    
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
    
    if 'derived_variables' not in st.session_state:
        st.session_state.derived_variables = {}
    
    if 'header_to_var_mapping' not in st.session_state:
        st.session_state.header_to_var_mapping = {}
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
    
    # Check if formulas exist
    if not st.session_state.formulas:
        st.error("❌ No formulas found. Please go back to the extraction page and extract formulas first.")
        st.info("💡 Use the sidebar navigation to return to the main page.")
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
                            # Prepare ALL variables (output, input, derived)
                            INPUT_VARIABLES = {
                                'TERM_START_DATE': 'Date when the policy starts',
                                'FUP_Date': 'First Unpaid Premium date',
                                'ENTRY_AGE': 'Age at policy inception',
                                'TOTAL_PREMIUM': 'Annual Premium amount',
                                'BOOKING_FREQUENCY': 'Frequency of premium booking',
                                'PREMIUM_TERM': 'Premium Paying Term',
                                'SUM_ASSURED': 'Sum Assured',
                                'Income_Benefit_Amount': 'Amount of income benefit',
                                'Income_Benefit_Frequency': 'Frequency of income benefit',
                                'DATE_OF_SURRENDER': 'Date when policy is surrendered',
                                'no_of_premium_paid': 'Years since commencement till FUP',
                                'maturity_date': 'Maturity date',
                                'policy_year': 'Years since commencement + 1',
                                'BENEFIT_TERM': 'Benefit term in years',
                                'GSV_FACTOR': 'GSV Factor',
                                'SSV1_FACTOR': 'SSV1 Factor',
                                'SSV2_FACTOR': 'SSV2 Factor',
                                'SSV3_FACTOR': 'SSV3 Factor',
                                'FUND_VALUE': 'Fund value at surrender/maturity',
                                'N': 'min(Policy_term, 20) - Elapsed_policy_duration',
                                'SYSTEM_PAID': 'Amount paid by system',
                                'CAPITAL_UNITS_VALUE': 'Units in policy fund',
                            }
                            
                            DERIVED_VARIABLES = {
                                'Elapsed_policy_duration': 'Years passed since policy start',
                                'CAPITAL_FUND_VALUE': 'Total fund value with bonuses',
                                'FUND_FACTOR': 'Fund value computation factor',
                                'Final_surrender_value_paid': 'Final surrender value',
                            }
                            
                            # Combine all variables for matching
                            combined_variables = list(all_variables) + list(INPUT_VARIABLES.keys()) + list(DERIVED_VARIABLES.keys())
                            # Remove duplicates
                            combined_variables = list(set(combined_variables))
                            
                            matcher = VariableHeaderMatcher()
                            mappings = matcher.match_all_variables(
                                combined_variables,
                                headers,
                                use_ai=False  # No AI in initial pass
                            )
                            
                            # Build a complete variable type dict
                            all_var_types = {}
                            for var in all_variables:
                                all_var_types[var] = 'output'
                            for var in INPUT_VARIABLES.keys():
                                all_var_types[var] = 'input'
                            for var in DERIVED_VARIABLES.keys():
                                all_var_types[var] = 'derived'

                            # Pass this to the matcher
                            matcher = VariableHeaderMatcher()
                            mappings = matcher.match_all_variables(
                                combined_variables,
                                headers,
                                all_variables=all_var_types,  # Pass the type dict
                                use_ai=False
                            )

                            # Store ALL mappings properly
                            st.session_state.variable_mappings = {}
                            st.session_state.header_to_var_mapping = {}

                            for var_name, mapping in mappings.items():
                                var_type = all_var_types.get(var_name, 'output')
                                
                                # Update mapping with correct type
                                mapping.variable_type = var_type
                                
                                # Store in variable_mappings
                                st.session_state.variable_mappings[var_name] = mapping
                                
                                # Store in header_to_var_mapping if mapped
                                if mapping.mapped_header:
                                    st.session_state.header_to_var_mapping[mapping.mapped_header] = f"[{var_type.upper()}] {var_name}"
                            
                            st.session_state.initial_mapping_done = True
                            
                            # Show matching statistics
                            total = len(combined_variables)
                            mapped = len([m for m in mappings.values() if m.mapped_header])
                            output_mapped = len([m for m in st.session_state.variable_mappings.values() if m.mapped_header])
                            input_derived_mapped = mapped - output_mapped
                            
                            # Count by method
                            method_counts = {}
                            for m in mappings.values():
                                if m.mapped_header:
                                    method = m.matching_method.split('_')[0]
                                    method_counts[method] = method_counts.get(method, 0) + 1
                            
                            st.success(f"✅ Mapped {mapped} out of {total} variables ({output_mapped} output, {input_derived_mapped} input/derived)")
                            
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
        with st.expander("➕ Add Custom Formula for Mapping", expanded=False):
            with st.form("custom_formula_form"):
                col_cf1, col_cf2 = st.columns([1, 2])
                with col_cf1:
                    custom_name = st.text_input("Name", placeholder="Custom_Calculation")
                with col_cf2:
                    custom_expr = st.text_input("Expression", placeholder="variable1 + variable2 * 0.5")
                
                submitted = st.form_submit_button("Add Formula", use_container_width=True)
                if submitted and custom_name and custom_expr:
                    st.session_state.custom_formulas.append({
                        'formula_name': custom_name,
                        'formula_expression': custom_expr
                    })
                    st.success(f"✅ Added: {custom_name}")
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
        st.subheader("🔗 Complete Mapping Interface")
        st.markdown("Map all Excel headers to variables (output, input, or derived). Use AI assistance or manual selection.")
        
        # Create tabs for different views
        tab1, tab2 = st.tabs(["📋 All Excel Headers", "📐 Input & Derived Variables"])
        
        with tab1:
            st.markdown("#### Excel Headers → Variables Mapping")
            st.info("💡 Map each header to a variable (output/input/derived). Use dropdown or AI assist.")
            
            # Prepare all available variables for mapping
            all_available_vars = list(st.session_state.variable_mappings.keys())
            
            # Add input variables
            INPUT_VARIABLES = {
                'TERM_START_DATE': 'Date when the policy starts',
                'FUP_Date': 'First Unpaid Premium date',
                'ENTRY_AGE': 'Age at policy inception',
                'TOTAL_PREMIUM': 'Annual Premium amount',
                'BOOKING_FREQUENCY': 'Frequency of premium booking',
                'PREMIUM_TERM': 'Premium Paying Term',
                'SUM_ASSURED': 'Sum Assured',
                'Income_Benefit_Amount': 'Amount of income benefit',
                'Income_Benefit_Frequency': 'Frequency of income benefit',
                'DATE_OF_SURRENDER': 'Date when policy is surrendered',
                'no_of_premium_paid': 'Years since commencement till FUP',
                'maturity_date': 'Maturity date',
                'policy_year': 'Years since commencement + 1',
                'BENEFIT_TERM': 'Benefit term in years',
                'GSV_FACTOR': 'GSV Factor',
                'SSV1_FACTOR': 'SSV1 Factor',
                'SSV2_FACTOR': 'SSV2 Factor',
                'SSV3_FACTOR': 'SSV3 Factor',
                'FUND_VALUE': 'Fund value at surrender/maturity',
                'N': 'min(Policy_term, 20) - Elapsed_policy_duration',
                'SYSTEM_PAID': 'Amount paid by system',
                'CAPITAL_UNITS_VALUE': 'Units in policy fund',
            }
            
            DERIVED_VARIABLES = {
                'Elapsed_policy_duration': 'Years passed since policy start',
                'CAPITAL_FUND_VALUE': 'Total fund value with bonuses',
                'FUND_FACTOR': 'Fund value computation factor',
                'Final_surrender_value_paid': 'Final surrender value',
            }
            
            # Combine all for dropdown
            all_vars_with_types = {}
            for var in all_available_vars:
                all_vars_with_types[f"[OUTPUT] {var}"] = "output"
            for var in INPUT_VARIABLES.keys():
                all_vars_with_types[f"[INPUT] {var}"] = "input"
            for var in DERIVED_VARIABLES.keys():
                all_vars_with_types[f"[DERIVED] {var}"] = "derived"
            
            # Create display names without prefixes
            # Create display names without type tags - just variable names
            dropdown_display = ["(Unmapped)"]
            dropdown_to_full = {"(Unmapped)": "(Unmapped)"}
            for var_full in sorted(all_vars_with_types.keys()):
                var_clean = var_full.split("] ", 1)[1] if "] " in var_full else var_full
                # Just use the clean variable name without type
                dropdown_display.append(var_clean)
                dropdown_to_full[var_clean] = var_full

            # Remove duplicates while preserving order
            seen = set()
            dropdown_display_unique = []
            for item in dropdown_display:
                if item not in seen:
                    seen.add(item)
                    dropdown_display_unique.append(item)
            dropdown_display = dropdown_display_unique
                        
            # Initialize header_to_var_mapping in session state
            if 'header_to_var_mapping' not in st.session_state:
                st.session_state.header_to_var_mapping = {}
                # Pre-populate from existing mappings
                for var_name, mapping in st.session_state.variable_mappings.items():
                    if mapping.mapped_header:
                        st.session_state.header_to_var_mapping[mapping.mapped_header] = f"[OUTPUT] {var_name}"
            
            # Compact table header
            col_h1, col_h2, col_h3, col_h4 = st.columns([2, 3, 2, 1])
            with col_h1:
                st.markdown("**Excel Header**")
            with col_h2:
                st.markdown("**Maps To Variable**")
            with col_h3:
                st.markdown("**Type**")
            with col_h4:
                st.markdown("**Actions**")
            
            st.markdown('<hr style="margin: 0.5rem 0; border: 0; border-top: 2px solid #004DA8;">', unsafe_allow_html=True)
            
            # Sort headers: mapped first
            sorted_headers = sorted(
                st.session_state.excel_headers,
                key=lambda h: (h not in st.session_state.header_to_var_mapping, h)
            )
            
            for header in sorted_headers:
                col1, col2, col3, col4 = st.columns([2, 3, 2, 1])
                
                with col1:
                    # Show header name with status indicator
                    is_mapped = header in st.session_state.header_to_var_mapping
                    status_icon = "✅" if is_mapped else "⚪"
                    st.markdown(f"{status_icon} `{header}`")
                
                with col2:
                    # Dropdown for variable selection
                    current_mapping_full = st.session_state.header_to_var_mapping.get(header, "(Unmapped)")
                    current_index = 0
                    
                    # Find display name for current mapping
                    current_display = "(Unmapped)"
                    for display, full in dropdown_to_full.items():
                        if full == current_mapping_full:
                            current_display = display
                            break
                    
                    if current_display in dropdown_display:
                        current_index = dropdown_display.index(current_display)

                    selected_var = st.selectbox(
                        "Variable",
                        options=dropdown_display,
                        index=current_index,
                        key=f"map_header_{header}",
                        label_visibility="collapsed"
                    )
                    
                    # Update mapping - convert display back to full name
                    selected_var_full = dropdown_to_full.get(selected_var, selected_var)
                    
                    if selected_var_full == "(Unmapped)":
                        if header in st.session_state.header_to_var_mapping:
                            del st.session_state.header_to_var_mapping[header]
                    else:
                        st.session_state.header_to_var_mapping[header] = selected_var_full
                
                with col3:
                    # Show variable type
                    if header in st.session_state.header_to_var_mapping:
                        var_display = st.session_state.header_to_var_mapping[header]
                        var_type = all_vars_with_types.get(var_display, "unknown")
                        st.markdown(f"*{var_type.upper()}*")
                    else:
                        st.markdown("*-*")
                
                with col4:
                    # Actions - AI or Remove
                    if header in st.session_state.header_to_var_mapping:
                        # Show remove button
                        if st.button("🗑️", key=f"delete_{header}", help="Remove mapping"):
                            del st.session_state.header_to_var_mapping[header]
                            st.rerun()
                    else:
                        # Show AI button
                        if st.button("🤖", key=f"ai_{header}", help="AI suggest", disabled=MOCK_MODE):
                            with st.spinner(f"AI analyzing..."):
                                matcher = VariableHeaderMatcher()
                                
                                # Create dict of clean var names to types
                                var_dict = {}
                                for var_full, var_type in all_vars_with_types.items():
                                    clean_var = var_full.split("] ", 1)[1] if "] " in var_full else var_full
                                    var_dict[clean_var] = var_type
                                
                                # Use efficient AI matching
                                best_var, var_type, score = matcher.ai_match_single_header(header, var_dict)
                                
                                if best_var and score >= CONFIDENCE_THRESHOLDS['low']:
                                    # Reconstruct full variable name with prefix
                                    best_match = f"[{var_type.upper()}] {best_var}"
                                    st.session_state.header_to_var_mapping[header] = best_match
                                    st.toast(f"✅ Mapped to {best_var} ({score:.2f})", icon="✅")
                                    st.rerun()
                                else:
                                    st.toast("No good match found", icon="⚠️")
                
                st.markdown('<hr style="margin: 0.3rem 0; border: 0; border-top: 1px solid #e0e0e0;">', unsafe_allow_html=True)
            
            # Summary stats - more compact
            st.markdown("---")
            col_stat1, col_stat2, col_stat3 = st.columns(3)
            
            mapped_count = len(st.session_state.header_to_var_mapping)
            total_headers = len(st.session_state.excel_headers)
            completion = int((mapped_count / total_headers) * 100) if total_headers > 0 else 0
            
            with col_stat1:
                st.metric("Total Headers", total_headers)
            with col_stat2:
                st.metric("Mapped", f"{mapped_count}/{total_headers}")
            with col_stat3:
                st.metric("Completion", f"{completion}%")
        
        with tab2:
            st.markdown("#### Input & Derived Variables Reference")
            
            # Define variables here to avoid scope issues
            INPUT_VARIABLES = {
                'TERM_START_DATE': 'Date when the policy starts',
                'FUP_Date': 'First Unpaid Premium date',
                'ENTRY_AGE': 'Age at policy inception',
                'TOTAL_PREMIUM': 'Annual Premium amount',
                'BOOKING_FREQUENCY': 'Frequency of premium booking',
                'PREMIUM_TERM': 'Premium Paying Term',
                'SUM_ASSURED': 'Sum Assured',
                'Income_Benefit_Amount': 'Amount of income benefit',
                'Income_Benefit_Frequency': 'Frequency of income benefit',
                'DATE_OF_SURRENDER': 'Date when policy is surrendered',
                'no_of_premium_paid': 'Years since commencement till FUP',
                'maturity_date': 'Maturity date',
                'policy_year': 'Years since commencement + 1',
                'BENEFIT_TERM': 'Benefit term in years',
                'GSV_FACTOR': 'GSV Factor',
                'SSV1_FACTOR': 'SSV1 Factor',
                'SSV2_FACTOR': 'SSV2 Factor',
                'SSV3_FACTOR': 'SSV3 Factor',
                'FUND_VALUE': 'Fund value at surrender/maturity',
                'N': 'min(Policy_term, 20) - Elapsed_policy_duration',
                'SYSTEM_PAID': 'Amount paid by system',
                'CAPITAL_UNITS_VALUE': 'Units in policy fund',
            }
            
            DERIVED_VARIABLES = {
                'Elapsed_policy_duration': 'Years passed since policy start',
                'CAPITAL_FUND_VALUE': 'Total fund value with bonuses',
                'FUND_FACTOR': 'Fund value computation factor',
                'Final_surrender_value_paid': 'Final surrender value',
            }
            
            # Input Variables Table
            st.markdown("**Input Variables**")
            input_data = []
            for var, desc in INPUT_VARIABLES.items():
                mapped_headers = [h for h, v in st.session_state.header_to_var_mapping.items() 
                                if v == f"[INPUT] {var}"]
                status = "✅ " + ", ".join(mapped_headers) if mapped_headers else "⚪ Not mapped"
                input_data.append({
                    "Variable": var,
                    "Description": desc,
                    "Mapping Status": status
                })
            
            input_df = pd.DataFrame(input_data)
            st.dataframe(input_df, use_container_width=True, hide_index=True)
            
            st.markdown("---")
            
            # Derived Variables Table
            st.markdown("**Derived Variables**")
            derived_data = []
            for var, desc in DERIVED_VARIABLES.items():
                mapped_headers = [h for h, v in st.session_state.header_to_var_mapping.items() 
                                if v == f"[DERIVED] {var}"]
                status = "✅ " + ", ".join(mapped_headers) if mapped_headers else "⚪ Not mapped"
                derived_data.append({
                    "Variable": var,
                    "Description": desc,
                    "Mapping Status": status
                })
            
            derived_df = pd.DataFrame(derived_data)
            st.dataframe(derived_df, use_container_width=True, hide_index=True)
            
            st.markdown("---")
            
        
        # Summary statistics
        st.markdown("---")
        col_stat1, col_stat2, col_stat3, col_stat4 = st.columns(4)
        
        total_headers = len(st.session_state.excel_headers)
        mapped_headers = len(st.session_state.header_to_var_mapping)
        output_vars_mapped = len([v for v, m in st.session_state.variable_mappings.items() if m.mapped_header])
        total_output_vars = len(st.session_state.variable_mappings)
        
        with col_stat1:
            st.metric("Total Headers", total_headers)
        with col_stat2:
            st.metric("Mapped Headers", mapped_headers)
        with col_stat3:
            st.metric("Output Vars Mapped", f"{output_vars_mapped}/{total_output_vars}")
        with col_stat4:
            completion = int((mapped_headers / total_headers) * 100) if total_headers > 0 else 0
            st.metric("Completion", f"{completion}%")
        
        # Proceed button
        st.markdown("---")
        col_btn1, col_btn2, col_btn3 = st.columns([1, 1, 1])
        
        with col_btn1:
            if st.button("✅ Confirm Mappings & View Formulas", type="primary"):
                # Sync header_to_var_mapping back to variable_mappings
                # Clear existing mappings
                for var_name in st.session_state.variable_mappings:
                    st.session_state.variable_mappings[var_name].mapped_header = ""
                    st.session_state.variable_mappings[var_name].is_verified = False
                
                # Update from header_to_var_mapping
                for header, var_display in st.session_state.header_to_var_mapping.items():
                    # Extract clean variable name
                    clean_var = var_display.split("] ", 1)[1] if "] " in var_display else var_display
                    
                    # Update if it's an output variable
                    if clean_var in st.session_state.variable_mappings:
                        st.session_state.variable_mappings[clean_var].mapped_header = header
                        st.session_state.variable_mappings[clean_var].is_verified = True
                        st.session_state.variable_mappings[clean_var].matching_method = "manual_updated"
                
                # Check unmapped output variables
                unmapped = [v for v, m in st.session_state.variable_mappings.items() if not m.mapped_header]
                
                if unmapped:
                    st.warning(f"⚠️ {len(unmapped)} output variables not mapped: {', '.join(unmapped[:5])}{'...' if len(unmapped) > 5 else ''}")
                    st.info("You can proceed anyway, but unmapped variables won't be replaced in formulas.")
                
                st.session_state.mapping_complete = True
                st.success("✅ Mappings confirmed!")
                st.rerun()
        
        with col_btn2:
            # Export all mappings
            mapping_export = {
                'header_to_variable': st.session_state.header_to_var_mapping,
                'variable_mappings': {
                    var: {
                        'mapped_header': m.mapped_header,
                        'confidence': m.confidence_score,
                        'method': m.matching_method,
                        'verified': m.is_verified
                    }
                    for var, m in st.session_state.variable_mappings.items()
                }
            }
            
            st.download_button(
                label="📥 Export All Mappings",
                data=json.dumps(mapping_export, indent=2),
                file_name="complete_mappings.json",
                mime="application/json"
            )
        
        with col_btn3:
            # Bulk AI mapping for unmapped headers
            if st.button("🤖 AI Map All Unmapped", disabled=MOCK_MODE):
                unmapped_headers = [h for h in st.session_state.excel_headers 
                                  if h not in st.session_state.header_to_var_mapping]
                
                if unmapped_headers:
                    with st.spinner(f"Running AI on {len(unmapped_headers)} unmapped headers..."):
                        # Need to recreate all_vars_with_types here
                        INPUT_VARIABLES = {
                            'TERM_START_DATE': 'Date when the policy starts',
                            'FUP_Date': 'First Unpaid Premium date',
                            'ENTRY_AGE': 'Age at policy inception',
                            'TOTAL_PREMIUM': 'Annual Premium amount',
                            'BOOKING_FREQUENCY': 'Frequency of premium booking',
                            'PREMIUM_TERM': 'Premium Paying Term',
                            'SUM_ASSURED': 'Sum Assured',
                            'Income_Benefit_Amount': 'Amount of income benefit',
                            'Income_Benefit_Frequency': 'Frequency of income benefit',
                            'DATE_OF_SURRENDER': 'Date when policy is surrendered',
                            'no_of_premium_paid': 'Years since commencement till FUP',
                            'maturity_date': 'Maturity date',
                            'policy_year': 'Years since commencement + 1',
                            'BENEFIT_TERM': 'Benefit term in years',
                            'GSV_FACTOR': 'GSV Factor',
                            'SSV1_FACTOR': 'SSV1 Factor',
                            'SSV2_FACTOR': 'SSV2 Factor',
                            'SSV3_FACTOR': 'SSV3 Factor',
                            'FUND_VALUE': 'Fund value at surrender/maturity',
                            'N': 'min(Policy_term, 20) - Elapsed_policy_duration',
                            'SYSTEM_PAID': 'Amount paid by system',
                            'CAPITAL_UNITS_VALUE': 'Units in policy fund',
                        }
                        
                        DERIVED_VARIABLES = {
                            'Elapsed_policy_duration': 'Years passed since policy start',
                            'CAPITAL_FUND_VALUE': 'Total fund value with bonuses',
                            'FUND_FACTOR': 'Fund value computation factor',
                            'Final_surrender_value_paid': 'Final surrender value',
                        }
                        
                        all_available_vars = list(st.session_state.variable_mappings.keys())
                        all_vars_with_types_ai = {}
                        for var in all_available_vars:
                            all_vars_with_types_ai[f"[OUTPUT] {var}"] = "output"
                        for var in INPUT_VARIABLES.keys():
                            all_vars_with_types_ai[f"[INPUT] {var}"] = "input"
                        for var in DERIVED_VARIABLES.keys():
                            all_vars_with_types_ai[f"[DERIVED] {var}"] = "derived"
                        
                        matcher = VariableHeaderMatcher()
                        progress = st.progress(0)
                        
                        # Create dict for efficient AI matching
                        var_dict = {}
                        for var_full, var_type in all_vars_with_types_ai.items():
                            clean_var = var_full.split("] ", 1)[1] if "] " in var_full else var_full
                            var_dict[clean_var] = var_type
                        
                        for idx, header in enumerate(unmapped_headers):
                            # Use efficient AI matching
                            best_var, var_type, score = matcher.ai_match_single_header(header, var_dict)
                            
                            if best_var and score >= CONFIDENCE_THRESHOLDS['low']:
                                best_match = f"[{var_type.upper()}] {best_var}"
                                st.session_state.header_to_var_mapping[header] = best_match
                            
                            progress.progress((idx + 1) / len(unmapped_headers))
                        
                        progress.empty()
                        st.success(f"✅ Mapped {len(unmapped_headers)} headers!")
                        st.rerun()
                else:
                    st.info("All headers are already mapped!")
            
    
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