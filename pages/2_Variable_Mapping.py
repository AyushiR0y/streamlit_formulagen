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
import time
import io
from functools import lru_cache

@lru_cache(maxsize=1)
def get_all_master_variables_cached():
    """Cached version - only recalculates when session changes"""
    return get_all_master_variables()
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
    'high': 0.90,    # Must be very similar
    'medium': 0.80,  # Good similarity
    'low': 0.75       # Minimum similarity to accept a match (Raised from 0.40)
}
REFERENCE_MAPPING_DICT = {
    # Premium-related
    'rop_benefit': 'TOTAL_PREMIUM_PAID',
    'rop benefit': 'TOTAL_PREMIUM_PAID',
    'return of premium': 'TOTAL_PREMIUM_PAID',
    'total premium paid': 'TOTAL_PREMIUM_PAID',
    'premium paid': 'TOTAL_PREMIUM_PAID',
    'full term premium': 'FULL_TERM_PREMIUM',
    'annual premium': 'FULL_TERM_PREMIUM',
    'yearly premium': 'FULL_TERM_PREMIUM',
    'total premium': 'TOTAL_PREMIUM_PAID',  # Generic - may need manual review
    
    # Surrender values
    'gsv': 'GSV',
    'guaranteed surrender value': 'GSV',
    'ssv': 'SSV',
    'special surrender value': 'SSV',
    'ssv1': 'SSV1_AMT',
    'ssv2': 'SSV2_AMT',
    'ssv3': 'SSV3_AMT',
    'surrender paid amount': 'SURRENDER_PAID_AMOUNT',
    'surrender value paid': 'SURRENDER_PAID_AMOUNT',
    
    # Sum assured
    'sa': 'SUM_ASSURED',
    'sum assured': 'SUM_ASSURED',
    'sum assured on death': 'SUM_ASSURED_ON_DEATH',
    'paid up sa': 'PAID_UP_SA',
    'paid up sum assured': 'PAID_UP_SA',
    
    # Dates
    'term start date': 'TERM_START_DATE',
    'policy start date': 'TERM_START_DATE',
    'commencement date': 'TERM_START_DATE',
    'fup date': 'FUP_Date',
    'first unpaid premium date': 'FUP_Date',
    'surrender date': 'DATE_OF_SURRENDER',
    'maturity date': 'maturity_date',
    
    # Other
    'entry age': 'ENTRY_AGE',
    'age at entry': 'ENTRY_AGE',
    'premium term': 'PREMIUM_TERM',
    'benefit term': 'BENEFIT_TERM',
    'policy year': 'policy_year',
    'fund value': 'FUND_VALUE',
}

# Updated INPUT_VARIABLES with clear descriptions
INPUT_VARIABLES = {
    'TERM_START_DATE': 'Date when the policy starts',
    'FUP_Date': 'First Unpaid Premium date',
    'ENTRY_AGE': 'Age of the policyholder at policy inception',
    'FULL_TERM_PREMIUM': 'Annual/Yearly Premium Amount (NOT total paid)',
    'TOTAL_PREMIUM': 'Generic premium field (annual premium amount)',
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
    'SYSTEM_PAID': 'Amount paid by system for surrender or maturity',
    'CAPITAL_UNITS_VALUE': 'Number of units in policy fund at surrender or maturity',
}

# Add these to DEFAULT_TARGET_OUTPUT_VARIABLES (after line 76)
DEFAULT_TARGET_OUTPUT_VARIABLES = [
    'TOTAL_PREMIUM_PAID',  # = FULL_TERM_PREMIUM × no_of_premium_paid
    'FULL_TERM_PREMIUM',   # Annual/Yearly premium amount
    'TEN_TIMES_AP', 
    'one_oh_five_percent_total_premium',
    'SUM_ASSURED_ON_DEATH', 
    'SUM_ASSURED', 
    'GSV', 
    'PAID_UP_SA',
    'PAID_UP_SA_ON_DEATH', 
    'paid_up_income_benefit_amount',
    'SSV1_AMT', 
    'SSV2_AMT', 
    'SSV3_AMT', 
    'SSV', 
    'SURRENDER_PAID_AMOUNT',
]


BASIC_DERIVED_FORMULAS = {
    'no_of_premium_paid': 'Calculate based on difference between TERM_START_DATE and FUP_Date',
    'policy_year': 'Calculate based on difference between TERM_START_DATE and DATE_OF_SURRENDER + 1',
    'maturity_date': 'TERM_START_DATE + (BENEFIT_TERM* 12) months',
    'Final_surrender_value_paid': 'Final surrender value paid',
    'Elapsed_policy_duration': 'How many years have passed since policy start',
    'CAPITAL_FUND_VALUE': 'Total policy fund value including bonuses',
    'FUND_FACTOR': 'Factor to compute fund value based on premiums and term'
}

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
            # Use io.BytesIO instead of pd.io.common.BytesIO
            df = pd.read_csv(io.BytesIO(file_bytes))
        else:
            df = pd.read_excel(io.BytesIO(file_bytes))
        
        headers = df.columns.tolist()
        return df, headers
    
    except Exception as e:
        st.error(f"Error loading Excel file: {e}")
        return None, []
def get_all_master_variables():
    """Aggregates variables from Input, Formula, Derived, and Custom sources"""
    all_vars = set()
    
    # 1. Static Input Variables
    all_vars.update(INPUT_VARIABLES.keys())
    
    # 2. Basic Derived Formulas
    all_vars.update(BASIC_DERIVED_FORMULAS.keys())
    
    # 3. Default Target Output Variables
    all_vars.update(DEFAULT_TARGET_OUTPUT_VARIABLES)
    
    # 4. Extracted from Main Formulas
    if 'formulas' in st.session_state and st.session_state.formulas:
        formula_vars, derived_defs = extract_variables_from_formulas(st.session_state.formulas)
        all_vars.update(formula_vars)
        all_vars.update(derived_defs.keys())
    
    # 5. Extracted from Custom Formulas
    if 'custom_formulas' in st.session_state and st.session_state.custom_formulas:
        cf_vars, cf_derived = extract_variables_from_formulas(st.session_state.custom_formulas)
        all_vars.update(cf_vars)
        all_vars.update(cf_derived.keys())
    
    # 6. Filter out excluded variables
    if 'excluded_variables' in st.session_state:
        all_vars = all_vars - st.session_state.excluded_variables
            
    return sorted(list(all_vars))

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
        self.user_verified_mappings: Dict[str, str] = {}  # Persisted mappings
        
    def normalize_text(self, text: str) -> str:
        """Normalize text for comparison"""
        text = text.lower()
        text = re.sub(r'[^a-z0-9\s]', ' ', text)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()
    
    def check_reference_dictionary(self, header: str) -> Optional[str]:
        """
        Check if header exists in reference dictionary.
        Returns matched variable or None.
        """
        normalized_header = self.normalize_text(header)
        
        # Direct lookup
        if normalized_header in REFERENCE_MAPPING_DICT:
            return REFERENCE_MAPPING_DICT[normalized_header]
        
        # Partial match (header contains reference key)
        for ref_key, var_name in REFERENCE_MAPPING_DICT.items():
            if ref_key in normalized_header or normalized_header in ref_key:
                return var_name
        
        return None
    
    def check_user_verified_mappings(self, header: str) -> Optional[str]:
        """Check if user has previously verified this mapping"""
        normalized_header = self.normalize_text(header)
        return self.user_verified_mappings.get(normalized_header)
    
    def expand_abbreviations(self, text: str) -> str:
        """Expand common insurance/financial abbreviations"""
        abbreviations = {
            'sa': 'sum assured',
            'dob': 'date of birth',
            'fup': 'first unpaid premium',
            'gsv': 'guaranteed surrender value',
            'ssv': 'special surrender value',
            'ssv1': 'special surrender value 1',
            'ssv2': 'special surrender value 2',
            'ssv3': 'special surrender value 3',
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
        """Calculate fuzzy string similarity with improved length penalty"""
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
        
        # Length penalty logic (unchanged from original)
        len_var = len(var_expanded)
        len_header = len(header_expanded)
        len_diff = abs(len_var - len_header)
        
        if len_var == 1 or len_header == 1:
            if len_var != len_header:
                score = score * 0.01
            return score
        
        if len_var <= 3 or len_header <= 3:
            if len_diff > 2:
                score = score * 0.1
            return score
        
        if len_diff > 15:
            score = score * 0.3
        elif len_diff > 10:
            score = score * 0.5
        elif len_diff > 5:
            score = score * 0.8
            
        return score
    
    def semantic_similarity_ai_batch(self, headers: List[str], variables: List[str]) -> Dict[str, Tuple[str, float, str]]:
        """Single AI call to match ALL headers to variables"""
        if MOCK_MODE or not client:
            return {h: ("", 0.0, "AI unavailable") for h in headers}
        
        try:
            # Enhanced prompt with premium distinction
            prompt = f"""Match these Excel headers to variable names using syntactic and semantic similarity. Output ONLY a JSON object.

HEADERS: {headers}

VARIABLES: {variables}

IMPORTANT DISTINCTIONS:
1. "FULL_TERM_PREMIUM" = Annual/yearly premium amount (single payment)
2. "TOTAL_PREMIUM_PAID" = Cumulative amount paid over multiple years
3. Headers like "ROP_BENEFIT", "Return of Premium" should map to "TOTAL_PREMIUM_PAID"
4. Headers like "Annual Premium", "Yearly Premium" should map to "FULL_TERM_PREMIUM"
5. "Surrender Paid Amount" should map to "SURRENDER_PAID_AMOUNT"

Return a JSON object where each key is a header and value is an object with:
- "variable": best matching variable name or null if no good match
- "score": confidence 0.0-1.0 (1.0=exact, 0.9=substring, 0.7-0.8=semantic, 0.0=no match)
- "reason": brief explanation

Example output format:
{{
"ROP_BENEFIT": {{"variable": "TOTAL_PREMIUM_PAID", "score": 0.95, "reason": "ROP means return of total premiums paid"}},
"ANNUAL_PREMIUM": {{"variable": "FULL_TERM_PREMIUM", "score": 0.95, "reason": "Annual premium is yearly amount"}},
"RandomCol": {{"variable": null, "score": 0.0, "reason": "No match"}}
}}

Output ONLY valid JSON, no other text."""

            response = client.chat.completions.create(
                model=DEPLOYMENT_NAME,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=3000,
                temperature=0.0
            )
            
            response_text = response.choices[0].message.content.strip()
            
            # Remove markdown code blocks if present
            if response_text.startswith('```'):
                response_text = re.sub(r'^```json?\s*|\s*```', '', response_text, flags=re.MULTILINE).strip()
            
            try:
                mappings_json = json.loads(response_text)
            except json.JSONDecodeError as e:
                st.error(f"AI returned invalid JSON: {e}")
                return {h: ("", 0.0, "JSON parse error") for h in headers}
            
            # Convert to expected format
            results = {}
            for header in headers:
                if header in mappings_json:
                    mapping = mappings_json[header]
                    var = mapping.get('variable')
                    score = float(mapping.get('score', 0.0))
                    reason = mapping.get('reason', 'No reason')
                    
                    if var is None or var == 'null':
                        var = ""
                        score = 0.0
                    
                    results[header] = (var, score, reason)
                else:
                    results[header] = ("", 0.0, "Not in AI response")
            
            return results
            
        except Exception as e:
            st.error(f"AI matching error: {e}")
            return {h: ("", 0.0, f"Error: {str(e)[:50]}") for h in headers}

    def match_all_with_ai(self, headers: List[str], variables: List[str], use_ai: bool = True) -> Dict[str, VariableMapping]:
        """
        Enhanced matching with 3-tier strategy:
        1. Check user-verified mappings (if available)
        2. Check reference dictionary
        3. AI/Fuzzy matching
        """
        mappings = {}
        
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        # Statistics tracking
        stats = {
            'user_verified': 0,
            'reference_dict': 0,
            'ai_matched': 0,
            'fuzzy_matched': 0,
            'unmatched': 0
        }
        
        # Load user verified mappings if they exist
        if 'user_verified_mappings' in st.session_state:
            self.user_verified_mappings = st.session_state.user_verified_mappings
        
        status_text.text("Checking verified mappings and reference dictionary...")
        progress_bar.progress(0.1)
        
        # First pass: Check verified mappings and reference dictionary
        remaining_headers = []
        for header in headers:
            # Tier 1: User-verified mappings (highest priority)
            user_verified = self.check_user_verified_mappings(header)
            if user_verified:
                mappings[header] = VariableMapping(
                    variable_name=header,
                    mapped_header=user_verified,
                    confidence_score=1.0,
                    matching_method="User Verified",
                    is_verified=True
                )
                stats['user_verified'] += 1
                continue
            
            # Tier 2: Reference dictionary
            ref_match = self.check_reference_dictionary(header)
            if ref_match:
                mappings[header] = VariableMapping(
                    variable_name=header,
                    mapped_header=ref_match,
                    confidence_score=0.98,
                    matching_method="Reference Dictionary",
                    is_verified=False
                )
                stats['reference_dict'] += 1
                continue
            
            # If no match, add to remaining for AI/fuzzy processing
            remaining_headers.append(header)
        
        progress_bar.progress(0.3)
        
        # Second pass: AI/Fuzzy matching for remaining headers
        if remaining_headers:
            if use_ai and not MOCK_MODE and client:
                status_text.text(f"AI matching {len(remaining_headers)} remaining headers...")
                
                ai_results = self.semantic_similarity_ai_batch(remaining_headers, variables)
                progress_bar.progress(0.7)
                
                for header in remaining_headers:
                    ai_variable, ai_score, ai_reason = ai_results.get(header, ("", 0.0, "No AI response"))
                    
                    if ai_variable and ai_score >= 0.6:
                        mappings[header] = VariableMapping(
                            variable_name=header,
                            mapped_header=ai_variable,
                            confidence_score=ai_score,
                            matching_method=f"AI: {ai_reason[:50]}",
                            is_verified=False
                        )
                        stats['ai_matched'] += 1
                    else:
                        # Fallback to fuzzy
                        best_score = 0.0
                        best_candidate = None
                        
                        for candidate in variables:
                            fuzzy_score = self.fuzzy_similarity(header, candidate)
                            if fuzzy_score > best_score:
                                best_score = fuzzy_score
                                best_candidate = candidate
                        
                        if best_candidate and best_score >= 0.5:
                            mappings[header] = VariableMapping(
                                variable_name=header,
                                mapped_header=best_candidate,
                                confidence_score=best_score,
                                matching_method="Fuzzy (AI fallback)",
                                is_verified=False
                            )
                            stats['fuzzy_matched'] += 1
                        else:
                            mappings[header] = VariableMapping(
                                variable_name=header,
                                mapped_header="",
                                confidence_score=0.0,
                                matching_method="No match",
                                is_verified=False
                            )
                            stats['unmatched'] += 1
            else:
                # No AI - fuzzy only
                status_text.text("Using fuzzy matching...")
                for header in remaining_headers:
                    best_score = 0.0
                    best_candidate = None
                    
                    for candidate in variables:
                        score = self.fuzzy_similarity(header, candidate)
                        if score > best_score:
                            best_score = score
                            best_candidate = candidate
                    
                    if best_candidate and best_score >= 0.5:
                        mappings[header] = VariableMapping(
                            variable_name=header,
                            mapped_header=best_candidate,
                            confidence_score=best_score,
                            matching_method="Fuzzy",
                            is_verified=False
                        )
                        stats['fuzzy_matched'] += 1
                    else:
                        mappings[header] = VariableMapping(
                            variable_name=header,
                            mapped_header="",
                            confidence_score=0.0,
                            matching_method="No match",
                            is_verified=False
                        )
                        stats['unmatched'] += 1
        
        progress_bar.progress(1.0)
        status_text.empty()
        progress_bar.empty()
        
        # Display statistics
        st.success(f"✅ Mapping complete! Matched {len(mappings) - stats['unmatched']}/{len(headers)} headers")
        
        stats_df = pd.DataFrame([
            {"Method": "User Verified", "Count": stats['user_verified']},
            {"Method": "Reference Dictionary", "Count": stats['reference_dict']},
            {"Method": "AI Semantic", "Count": stats['ai_matched']},
            {"Method": "Fuzzy Matching", "Count": stats['fuzzy_matched']},
            {"Method": "Unmatched", "Count": stats['unmatched']},
        ])
        
        st.dataframe(stats_df, hide_index=True, use_container_width=True)
        
        return mappings

def add_missing_header_as_variable(variable_name: str, description: str = ""):
    """
    Add a variable as a header directly when the header is completely missing.
    This creates a synthetic column in the dataframe.
    """
    if 'excel_df' not in st.session_state or st.session_state.excel_df is None:
        st.error("No Excel file loaded!")
        return False
    
    # Add column with None values
    st.session_state.excel_df[variable_name] = None
    
    # Update headers list
    if variable_name not in st.session_state.excel_headers:
        st.session_state.excel_headers.append(variable_name)
    
    # Auto-map to itself
    st.session_state.header_to_var_mapping[variable_name] = variable_name
    
    # Track as user-added
    if 'user_added_headers' not in st.session_state:
        st.session_state.user_added_headers = {}
    
    st.session_state.user_added_headers[variable_name] = description
    
    return True
    
def apply_mappings_to_formulas(formulas: List[Dict], header_to_var_mapping: Dict[str, str]) -> List[Dict]:
    """
    Replace variables in formulas with mapped Excel Headers.
    header_to_var_mapping format: { "Excel_Header": "VariableName" }
    Formula: "VariableName * 2"
    Result: "[Excel_Header] * 2"
    """
    mapped_formulas = []
    
    # Create reverse mapping: variable -> header
    var_to_header = {var: header for header, var in header_to_var_mapping.items() if var}
    
    # Debug
    st.write(f"🔍 Applying {len(var_to_header)} mappings to formulas")
    
    for formula in formulas:
        expr = formula.get('formula_expression', '')
        original_expr = expr
        
        # Replace each variable with its corresponding header
        for var_name, excel_header in var_to_header.items():
            # Use word boundaries to avoid partial replacements
            pattern = r'\b' + re.escape(var_name) + r'\b'
            expr = re.sub(pattern, f'[{excel_header}]', expr)
        
        # Debug if changed
        if expr != original_expr:
            st.write(f"✅ Mapped: {formula.get('formula_name')}")
            st.write(f"   Before: {original_expr[:100]}")
            st.write(f"   After: {expr[:100]}")
        
        mapped_formulas.append({
            'formula_name': formula.get('formula_name', ''),
            'original_expression': formula.get('formula_expression', ''),
            'mapped_expression': expr
        })
    
    return mapped_formulas


def load_css(file_name="style.css"):
    """
    Loads CSS file. Automatically handles cases where the script
    is inside a 'pages' subdirectory by looking one level up.
    """
    # 1. Get the directory where this script is currently running from
    current_dir = os.path.dirname(os.path.abspath(__file__))
    
    # 2. Check if we are inside a 'pages' folder
    # If yes, we need to look one level up ('..') to find the CSS
    if os.path.basename(current_dir) == "pages":
        css_path = os.path.join(current_dir, "..", file_name)
    else:
        css_path = os.path.join(current_dir, file_name)
    
    # 3. Normalize the path (converts ".." to actual parent path)
    css_path = os.path.normpath(css_path)
    
    # 4. Load and inject the CSS
    if os.path.exists(css_path):
        with open(css_path, 'r') as f:
            st.markdown(f'<style>{f.read()}</style>', unsafe_allow_html=True)
    else:
        # If it still fails, show exactly where it looked
        st.error(f"⚠️ CSS file not found at: `{css_path}`. <br>Please ensure `style.css` is in the main project folder.", unsafe_allow_html=True)
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
                    Variable Mapping
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
        
    # State for user's variable selection
    if 'selected_variables_for_mapping' not in st.session_state:
        st.session_state.selected_variables_for_mapping = []
    
   

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
                    
                    # --- Start Automatic Mapping Button with Custom Styling ---
                    st.markdown("---")
                    
                    # Custom CSS for light green button with gradient hover
                    st.markdown("""
                        <style>
                        /* Target the specific button by its container */
                        button[key="start_mapping_btn"] {
                            background: #90EE90 !important;
                            color: white !important;
                            font-weight: 600 !important;
                            border: none !important;
                            transition: all 0.3s ease !important;
                        }
                        
                        button[key="start_mapping_btn"]:hover:not(:disabled) {
                            background: linear-gradient(135deg, #004DA8 0%, #0066CC 100%) !important;
                            transform: translateY(-2px) !important;
                            box-shadow: 0 6px 20px rgba(0, 77, 168, 0.4) !important;
                        }
                        
                        button[key="start_mapping_btn"]:disabled {
                            background: #cccccc !important;
                            cursor: not-allowed !important;
                            opacity: 0.6 !important;
                        }
                        
                        /* Alternative: target by testing the button text */
                        button:has(p:contains("Start Automatic Mapping")) {
                            background: #90EE90 !important;
                        }
                        </style>
                    """, unsafe_allow_html=True)
                    
                    if st.button("🔗 Start Automatic Mapping", 
                                type="primary", 
                                key="start_mapping_btn", 
                                disabled=st.session_state.initial_mapping_done,
                                help="Click to automatically match Excel headers with formula variables"):
                        
                        # Get active variables (excluding deleted ones)
                        active_variables = get_all_master_variables()
                        
                        # Determine if AI should be used
                        use_ai_mapping = not MOCK_MODE
                        
                        if use_ai_mapping:
                            st.info("🤖 AI-enhanced mapping enabled - using 3-stage process (Lexical → Fuzzy → AI Review)")
                        else:
                            st.info("📊 Using lexical and fuzzy matching only (AI not configured)")
                        
                        with st.spinner("Analyzing headers and matching with variables..."):
                            # 1. CLEAR OLD MAPPINGS
                            st.session_state.header_to_var_mapping = {}
                            
                            matcher = VariableHeaderMatcher()
                            
                            # 2. Map Headers -> Variables (with integrated AI)
                            mappings = matcher.match_all_with_ai(
                                headers=headers,
                                variables=active_variables,
                                use_ai=use_ai_mapping
                            )
                            
                            # 3. Update session state
                            new_mapping = {}
                            for header, mapping_obj in mappings.items():
                                if mapping_obj.mapped_header:
                                    new_mapping[header] = mapping_obj.mapped_header
                                else:
                                    new_mapping[header] = ""
                            
                            st.session_state.header_to_var_mapping = new_mapping
                            st.session_state.initial_mapping_done = True
                            
                            # Show matching statistics
                            total = len(mappings)
                            mapped = len([m for m in mappings.values() if m.mapped_header])
                            
                            # Count by method
                            method_counts = {}
                            for m in mappings.values():
                                if m.mapped_header:
                                    method = m.matching_method.replace('_', ' ').title()
                                    method_counts[method] = method_counts.get(method, 0) + 1
                            
                            st.success(f"✅ Mapped {mapped} out of {total} headers using {len(active_variables)} variables")
                            
                            if method_counts:
                                st.markdown("**Matching Methods Used:**")
                                method_df = pd.DataFrame([
                                    {"Method": method, "Count": count}
                                    for method, count in sorted(method_counts.items(), key=lambda x: -x[1])
                                ])
                                st.dataframe(method_df, hide_index=True, use_container_width=True)

    
    with col2:
        st.subheader("📋 Available Variables")
        st.markdown("Variables available for mapping: **Input**, **Derived**, and **Extracted** from formulas.")
        
        # Initialize excluded variables in session state
        if 'excluded_variables' not in st.session_state:
            st.session_state.excluded_variables = set()
        
        # Get consolidated variable list
        all_variables = get_all_master_variables()
        
        if all_variables:
            # Categorize variables for display
            input_vars = set(INPUT_VARIABLES.keys())
            formula_vars, derived_defs = extract_variables_from_formulas(st.session_state.formulas)
            
            var_df_data = []
            for var in sorted(all_variables):
                if var not in st.session_state.excluded_variables:
                    v_type = "Input"
                    if var in derived_defs:
                        v_type = "Derived"
                    elif var not in input_vars:
                        v_type = "Extracted"
                    var_df_data.append({'Variable Name': var, 'Type': v_type, 'Remove': False})
            
            var_df = pd.DataFrame(var_df_data)
            
            # Display dataframe with checkboxes using data_editor
            edited_df = st.data_editor(
                var_df,
                column_config={
                    "Variable Name": st.column_config.TextColumn("Variable Name", disabled=True),
                    "Type": st.column_config.TextColumn("Type", disabled=True),
                    "Remove": st.column_config.CheckboxColumn("Remove", help="Check to remove this variable")
                },
                hide_index=True,
                use_container_width=True,
                key="variables_editor"
            )
            
            # Process removals
            if edited_df['Remove'].any():
                vars_to_remove = edited_df[edited_df['Remove'] == True]['Variable Name'].tolist()
                if vars_to_remove:
                    st.session_state.excluded_variables.update(vars_to_remove)
                    st.rerun()
            
            # Show derived variable formulas
            if derived_defs:
                with st.expander("📐 Derived Variable Definitions", expanded=False):
                    for var, formula in sorted(derived_defs.items()):
                        if var not in st.session_state.excluded_variables:
                            st.markdown(f"**`{var}`** = `{formula}`")
        else:
            st.info("No variables detected.")
    
        # Make Add Custom Formula expandable and collapsed by default
        with st.expander("➕ Add Custom Formula", expanded=False):
            custom_name = st.text_input("Name", placeholder="Custom_Calc", key="cf_name")
            custom_expr = st.text_input("Expression", placeholder="var1 + var2 * 0.5", key="cf_expr")
            
            if st.button("Add Formula", key="add_cf"):
                if custom_name and custom_expr:
                    st.session_state.custom_formulas.append({
                        'formula_name': custom_name,
                        'formula_expression': custom_expr
                    })
                    st.success(f"✅ Added: {custom_name}")
                    st.rerun()
            
            if st.session_state.custom_formulas:
                st.markdown("---")
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
    if st.session_state.initial_mapping_done:
        st.markdown("---")
        st.subheader("🔗 Header to Variable Mappings")
        st.markdown("Review and edit the mappings. Rows represent **Excel Headers**. Map them to the **Variables** used in formulas.")
        
        # Debug: Show current mapping state
        with st.expander("🔍 Debug: Current Mapping State", expanded=False):
            st.json(st.session_state.header_to_var_mapping)
            st.write(f"Total mappings: {len(st.session_state.header_to_var_mapping)}")
            st.write(f"Non-empty mappings: {len([v for v in st.session_state.header_to_var_mapping.values() if v])}")
        
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
            
            # Create a form to batch updates
            with st.form(key="mapping_form"):
                updated_mappings = {}
                
                for header in active_headers:
                    # Get current mapping for this header
                    current_var = st.session_state.header_to_var_mapping.get(header, "")
                    
                    col1, col2, col3 = st.columns([3, 3, 1])

                    with col1:
                        st.text_input("Header", value=header, key=f"h_txt_{header}", label_visibility="collapsed", disabled=True)
                    
                    with col2:
                        # Dropdown options: (None) + all variables
                        dropdown_options = ["(None of the following)"] + current_variables
                        
                        # Determine index based on current mapping
                        if current_var and current_var in dropdown_options:
                            idx = dropdown_options.index(current_var)
                        else:
                            idx = 0  # Default to "(None of the following)"
                        
                        # Selectbox with unique key
                        new_var = st.selectbox(
                            "Variable",
                            options=dropdown_options,
                            index=idx,
                            key=f"var_select_{header}",
                            label_visibility="collapsed"
                        )
                        
                        # Store in temporary dict (will be applied on form submit)
                        final_var = "" if new_var == "(None of the following)" else new_var
                        updated_mappings[header] = final_var
                    
                    with col3:
                        # Remove checkbox instead of button (since we're in a form)
                        remove = st.checkbox("🗑️", key=f"remove_{header}", help="Remove this column")
                        if remove:
                            if header not in st.session_state.removed_headers:
                                st.session_state.removed_headers.append(header)
                                
                    st.markdown('<hr style="margin: 0.5rem 0; border: 0; border-top: 1px solid #e0e0e0;">', unsafe_allow_html=True)
                
                # Submit button for the form
                submitted = st.form_submit_button("💾 Save Changes", type="primary")
                
                if submitted:
                    # Apply all changes at once
                    for header, var in updated_mappings.items():
                        if header not in st.session_state.removed_headers:
                            st.session_state.header_to_var_mapping[header] = var
                    
                    st.success("✅ Changes saved!")
                    st.rerun()
        
        with tab2:
            st.markdown("#### Current Mappings (JSON)")
            
            # Filter out empty mappings and removed headers
            active_mapping = {
                h: v for h, v in st.session_state.header_to_var_mapping.items() 
                if v and h not in st.session_state.removed_headers
            }
            
            st.json(active_mapping)
            
            # Show statistics
            st.write(f"**Active mappings:** {len(active_mapping)}")
            st.write(f"**Removed headers:** {len(st.session_state.removed_headers)}")
            
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
        st.markdown("---")
        st.subheader("➕ Add Missing Variable as Header")
        st.markdown("If a required variable doesn't exist in your Excel file, you can add it here. You'll need to populate it later.")
        
        col_add1, col_add2, col_add3 = st.columns([2, 3, 1])
        
        with col_add1:
            new_header_var = st.selectbox(
                "Select Variable to Add",
                options=[""] + [v for v in get_all_master_variables() if v not in st.session_state.excel_headers],
                key="new_header_variable"
            )
        
        with col_add2:
            new_header_desc = st.text_input(
                "Description (optional)",
                placeholder="e.g., Will be calculated from other fields",
                key="new_header_desc"
            )
        
        with col_add3:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("➕ Add", key="add_missing_header", disabled=not new_header_var):
                if add_missing_header_as_variable(new_header_var, new_header_desc):
                    st.success(f"✅ Added '{new_header_var}' as a new column")
                    st.rerun()
        
        # Show user-added headers
        if 'user_added_headers' in st.session_state and st.session_state.user_added_headers:
            with st.expander("📋 User-Added Headers", expanded=False):
                for var, desc in st.session_state.user_added_headers.items():
                    st.markdown(f"**`{var}`**: {desc or '(no description)'}")
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
        
        
        
        # --- Confirm & Export Section ---
        st.markdown("---")
        col_btn1, col_btn2, col_btn3 = st.columns([1, 1, 1]) # Changed to 3 columns for JSON and CSV
        
        with col_btn1:
            if st.button("✅ Confirm & Save Mappings", type="primary", key="confirm_mappings_btn"):
                # Check for unmapped active headers
                unmapped = [h for h in active_headers if not st.session_state.header_to_var_mapping.get(h)]
                
                if unmapped:
                    st.warning(f"⚠️ {len(unmapped)} headers are unmapped: {', '.join(unmapped[:5])}{'...' if len(unmapped) > 5 else ''}")
                
                # Save verified mappings for future use
                if 'user_verified_mappings' not in st.session_state:
                    st.session_state.user_verified_mappings = {}
                
                # Store normalized mappings
                for header, var in st.session_state.header_to_var_mapping.items():
                    if var:  # Only save non-empty mappings
                        normalized_header = header.lower().strip()
                        st.session_state.user_verified_mappings[normalized_header] = var
                
                st.session_state.mapping_complete = True
                st.success(f"✅ Mappings confirmed and saved! {len(st.session_state.user_verified_mappings)} verified mappings stored.")
                st.rerun()
        
        with col_btn2:
            # Export JSON
            active_mapping = {
                h: v for h, v in st.session_state.header_to_var_mapping.items() 
                if v and h not in st.session_state.removed_headers
            }
            st.download_button(
                label="📥 Export JSON",
                data=json.dumps(active_mapping, indent=2),
                file_name="final_mappings.json",
                mime="application/json"
            )

        with col_btn3:
            # Export CSV
            df_mapping = pd.DataFrame(list(active_mapping.items()), columns=['Excel_Header', 'Mapped_Variable'])
            csv_data = df_mapping.to_csv(index=False)
            st.download_button(
                label="📥 Export CSV",
                data=csv_data,
                file_name="final_mappings.csv",
                mime="text/csv"
            )
    
    # Show mapped formulas
    if st.session_state.mapping_complete:
        st.markdown("---")
        st.subheader("📐 Formulas with Mapped Headers")
        st.markdown("Variables in formulas have been replaced by mapped Excel headers (shown in brackets).")
        
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
                label="📥 Download Formulas (JSON)",
                data=json.dumps(mapped_formulas, indent=2),
                file_name="mapped_formulas.json",
                mime="application/json"
            )
        
        with col_exp2:
            csv_formula_data = pd.DataFrame(mapped_formulas).to_csv(index=False)
            st.download_button(
                label="📥 Download Formulas (CSV)",
                data=csv_formula_data,
                file_name="mapped_formulas.csv",
                mime="text/csv"
            )
        
        with col_exp3:
            if st.button("➡️ Proceed to Calculations", type="primary", key="goto_calc"):
                st.switch_page("pages/3_Calculation_Engine.py")
    
    # Footer
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