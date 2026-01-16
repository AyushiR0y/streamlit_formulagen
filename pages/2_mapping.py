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
    'high': 0.90,    # Must be very similar
    'medium': 0.80,  # Good similarity
    'low': 0.75       # Minimum similarity to accept a match (Raised from 0.40)
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
        
        # --- IMPROVED LENGTH PENALTY ---
        # Penalize matches where one string is much shorter than the other
        # This prevents single letters like "N" from matching everything
        
        len_var = len(var_expanded)
        len_header = len(header_expanded)
        len_diff = abs(len_var - len_header)
        
        # Special case: Single character variables should ONLY match single character headers
        if len_var == 1 or len_header == 1:
            if len_var != len_header:
                score = score * 0.01  # 99% penalty
            return score
        
        # Very short variables (2-3 chars) need exact or very close matches
        if len_var <= 3 or len_header <= 3:
            if len_diff > 2:
                score = score * 0.1  # 90% penalty
            return score
        
        # General length difference penalties
        if len_diff > 15:
            score = score * 0.3  # 70% penalty
        elif len_diff > 10:
            score = score * 0.5  # 50% penalty
        elif len_diff > 5:
            score = score * 0.8  # 20% penalty
            
        return score
    
    def calculate_combined_score(self, var: str, header: str) -> Tuple[float, str]:
        """Calculate combined score from all non-AI methods"""
        lex_score = self.lexical_similarity(var, header)
        fuzzy_score = self.fuzzy_similarity(var, header)
        
        # Weight lexical matching more heavily as it's more reliable
        combined = (lex_score * 0.6) + (fuzzy_score * 0.4)
        
        # Determine which method contributed most
        if lex_score > fuzzy_score:
            method = "lexical"
        else:
            method = "fuzzy"
            
        return combined, method
    def semantic_similarity_ai(self, header: str, variables: List[str]) -> Tuple[str, float, str]:
        """
        Use AI to compare header against ALL variables and return the best match
        Returns: (best_variable, score, reason)
        """
        # Just call the batch method with a single header
        result = self.semantic_similarity_ai_batch([header], variables)
        return result.get(header, ("", 0.0, "No AI response"))
    def semantic_similarity_ai_batch(self, headers: List[str], variables: List[str]) -> Dict[str, Tuple[str, float, str]]:
        """
        Use AI to map multiple headers to variables in a single API call
        Returns: {header: (best_variable, score, reason)}
        """
        if MOCK_MODE or not client:
            return {h: ("", 0.0, "AI unavailable") for h in headers}
        
        try:
            # Format headers and variables for the prompt
            header_list = "\n".join([f"  {i+1}. {h}" for i, h in enumerate(headers)])
            var_list = "\n".join([f"  - {var}" for var in variables])
            
            prompt = f"""You are an expert in insurance and financial data mapping.

                Excel Headers to Map:
                {header_list}

                Available Variables:
                {var_list}

                Task: For EACH header above, find the BEST matching variable from the variable list.

                Consider:
                - Semantic meaning in insurance/financial context
                - Common abbreviations (SSV = Special Surrender Value, GSV = Guaranteed Surrender Value, etc.)
                - SSV1, SSV2, SSV3 are distinct from each other and SSV. Map accordingly.
                - Conceptual equivalence even if wording differs
                - Industry-standard terminology

                Respond for EACH header in this EXACT format:

                HEADER: <exact header name from the list>
                VARIABLE: <exact variable name from the list, or NONE if no good match>
                SCORE: <confidence score 0.0 to 1.0>
                REASON: <brief explanation>

                ---

                Repeat the above format for each header. If no variable is a good semantic match (score below 0.75), use VARIABLE: NONE"""

            response = client.chat.completions.create(
                model=DEPLOYMENT_NAME,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500 + (len(headers) * 100),  # Scale tokens with number of headers
                temperature=0.1
            )
            
            response_text = response.choices[0].message.content.strip()
            
            # Parse response for each header
            results = {}
            
            # Split by separator or header patterns
            sections = re.split(r'\n---\n|\n\nHEADER:', response_text)
            
            for section in sections:
                if not section.strip():
                    continue
                
                # Add back "HEADER:" if it was removed by split
                if not section.strip().startswith("HEADER:"):
                    section = "HEADER:" + section
                
                # Parse this section
                header_match = re.search(r'HEADER:\s*(.+?)(?:\n|$)', section, re.IGNORECASE)
                var_match = re.search(r'VARIABLE:\s*(.+?)(?:\n|$)', section, re.IGNORECASE)
                score_match = re.search(r'SCORE:\s*([0-9]*\.?[0-9]+)', section, re.IGNORECASE)
                reason_match = re.search(r'REASON:\s*(.+?)(?:\n---|\Z)', section, re.IGNORECASE | re.DOTALL)
                
                if header_match:
                    header = header_match.group(1).strip()
                    matched_var = var_match.group(1).strip() if var_match else "NONE"
                    score = float(score_match.group(1)) if score_match else 0.0
                    reason = reason_match.group(1).strip() if reason_match else "No explanation"
                    
                    # Validate header is in our list
                    if header in headers:
                        # If AI said NONE or variable not in our list, return empty
                        if matched_var == "NONE" or matched_var not in variables:
                            results[header] = ("", 0.0, reason)
                        else:
                            results[header] = (matched_var, min(score, 1.0), reason)
            
            # Fill in any missing headers with empty results
            for header in headers:
                if header not in results:
                    results[header] = ("", 0.0, "No AI response")
            
            return results
            
        except Exception as e:
            st.warning(f"AI batch semantic matching failed: {e}")
            return {h: ("", 0.0, f"AI_error: {str(e)}") for h in headers}
    
    
    def match_all_with_ai(self, headers: List[str], variables: List[str], use_ai: bool = True) -> Dict[str, VariableMapping]:
        """
        Three-stage matching process:
        1. Lexical + Fuzzy matching for initial mappings
        2. AI review and improvement of all mappings (if enabled)
        3. Return final mappings
        """
        mappings = {}
        
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        # Stage 1: Lexical + Fuzzy matching
        status_text.text("Stage 1/3: Running lexical and fuzzy matching...")
        progress_bar.progress(0.33)
        
        initial_mappings = {}
        for header in headers:
            best_score = 0.0
            best_candidate = None
            best_method = "no_match"
            
            # Try lexical and fuzzy matching
            for candidate in variables:
                lex_score = self.lexical_similarity(header, candidate)
                if lex_score > best_score:
                    best_score = lex_score
                    best_candidate = candidate
                    best_method = "lexical"
                
                fuzzy_score = self.fuzzy_similarity(header, candidate)
                if fuzzy_score > best_score:
                    best_score = fuzzy_score
                    best_candidate = candidate
                    best_method = "fuzzy"
            
            initial_mappings[header] = {
                'variable': best_candidate if best_score >= CONFIDENCE_THRESHOLDS['low'] else "",
                'score': best_score,
                'method': best_method
            }
        
        # Stage 2: AI Review and Improvement (if enabled and available)
        if use_ai and not MOCK_MODE and client:
            status_text.text("Stage 2/3: AI reviewing and improving mappings...")
            progress_bar.progress(0.66)
            
            try:
                # Get AI suggestions for ALL headers in one batch
                ai_results = self.semantic_similarity_ai_batch(headers, variables)
                
                # Compare AI results with initial mappings and use the better one
                for header in headers:
                    ai_variable, ai_score, ai_reason = ai_results.get(header, ("", 0.0, ""))
                    initial = initial_mappings[header]
                    
                    # Use AI result if:
                    # 1. AI found a match above threshold AND
                    # 2. AI score is higher than initial score OR initial had no match
                    if ai_variable and ai_score >= CONFIDENCE_THRESHOLDS['low']:
                        if ai_score > initial['score'] or not initial['variable']:
                            initial_mappings[header] = {
                                'variable': ai_variable,
                                'score': ai_score,
                                'method': f"ai_semantic"
                            }
            except Exception as e:
                st.warning(f"AI review encountered an error: {e}. Using lexical/fuzzy results.")
        else:
            status_text.text("Stage 2/3: Skipping AI review (not enabled)...")
            progress_bar.progress(0.66)
        
        # Stage 3: Create final mapping objects
        status_text.text("Stage 3/3: Finalizing mappings...")
        progress_bar.progress(1.0)
        
        for header, mapping_data in initial_mappings.items():
            mappings[header] = VariableMapping(
                variable_name=header,
                mapped_header=mapping_data['variable'],
                confidence_score=mapping_data['score'],
                matching_method=mapping_data['method'],
                is_verified=mapping_data['score'] >= CONFIDENCE_THRESHOLDS['high']
            )
        
        progress_bar.empty()
        status_text.empty()
        
        return mappings
        
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
        # AI compares header against ALL candidates at once
        if use_ai:
            ai_variable, ai_score, ai_reason = self.semantic_similarity_ai(target, candidates)
            
            if ai_score > best_score and ai_variable:
                best_score = ai_score
                best_candidate = ai_variable
                best_method = f"semantic_ai ({ai_reason[:50]}...)"
        
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




def get_all_master_variables():
    """Aggregates variables from Input, Formula, Derived, and AI-generated sources"""
    all_vars = set()
    
    # 1. Static Input Variables
    all_vars.update(INPUT_VARIABLES.keys())
    
    # 2. Extracted from Formulas (including AI-generated intermediate variables)
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
    
    # 4. AI-generated variables from calculation_steps
    # These are intermediate variables created by AI during formula extraction
    if 'formulas' in st.session_state and st.session_state.formulas:
        for formula in st.session_state.formulas:
            calc_steps = formula.get('calculation_steps', [])
            if isinstance(calc_steps, list):
                for step in calc_steps:
                    # Extract variable names from step descriptions
                    if isinstance(step, dict):
                        step_formula = step.get('formula', '')
                        # Look for patterns like "var_name = ..."
                        var_match = re.match(r'([a-zA-Z][a-zA-Z0-9_]*)\s*=', step_formula)
                        if var_match:
                            all_vars.add(var_match.group(1))
                    elif isinstance(step, str):
                        var_match = re.match(r'([a-zA-Z][a-zA-Z0-9_]*)\s*=', step)
                        if var_match:
                            all_vars.add(var_match.group(1))
            
    return sorted(list(all_vars))


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
                    
                    
                    # --- Filter Variables Step ---
                    st.markdown("---")
                    st.subheader("🛠️ Filter Variables for Mapping")
                    st.markdown("Deselect variables that are **not** relevant to this specific Excel file.")
                    
                    # When getting master variables for mapping
                    all_master_vars = get_all_master_variables()

                    # Filter out single-letter variables unless explicitly selected
                    # These require manual mapping due to ambiguity
                    single_letter_vars = ['N', 'M', 'X', 'Y', 'Z']
                    default_vars = [v for v in all_master_vars if v not in single_letter_vars]
                    
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
                        # Determine if AI should be used
                        use_ai_mapping = not MOCK_MODE
                        
                        if use_ai_mapping:
                            st.info("🤖 AI-enhanced mapping enabled - using 3-stage process (Lexical → Fuzzy → AI Review)")
                        else:
                            st.info("📊 Using lexical and fuzzy matching only (AI not configured)")
                        
                        with st.spinner("Analyzing headers and matching with variables..."):
                            # 1. CLEAR OLD MAPPINGS
                            st.session_state.header_to_var_mapping = {}
                            
                            active_variables = st.session_state.selected_variables_for_mapping
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
                
                # In your mapping table section, find where you create the selectbox and replace it with this:

                with col2:
                    # Dropdown options: (None) + all variables
                    dropdown_options = ["(None of the following)"] + current_variables
                    
                    # Get the current mapped variable from session state
                    current_var = st.session_state.header_to_var_mapping.get(header, "")
                    
                    # Determine index - IMPORTANT: This must recalculate every time
                    if current_var and current_var in dropdown_options:
                        idx = dropdown_options.index(current_var)
                    else:
                        idx = 0  # Default to "(None of the following)"
                    
                    # Use a unique key that includes the current value to force refresh
                    # This is the KEY FIX - adding current_var to the key forces widget recreation
                    widget_key = f"var_select_{header}_{current_var}"
                    
                    new_var = st.selectbox(
                        "Variable",
                        options=dropdown_options,
                        index=idx,
                        key=widget_key,
                        label_visibility="collapsed"
                    )
                    
                    # Update mapping if changed
                    final_var = "" if new_var == "(None of the following)" else new_var
                    
                    # Only update if different from what's in session state
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
        
        
        
        # --- Confirm & Export Section ---
        st.markdown("---")
        col_btn1, col_btn2, col_btn3 = st.columns([1, 1, 1]) # Changed to 3 columns for JSON and CSV
        
        with col_btn1:
            if st.button("✅ Confirm Mappings", type="primary", key="confirm_mappings_btn"):
                # Check for unmapped active headers
                unmapped = [h for h in active_headers if not st.session_state.header_to_var_mapping.get(h)]
                
                if unmapped:
                    st.warning(f"⚠️ {len(unmapped)} headers are unmapped: {', '.join(unmapped[:5])}{'...' if len(unmapped) > 5 else ''}")
                
                st.session_state.mapping_complete = True
                st.success("✅ Mappings confirmed!")
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
                st.switch_page("pages/3_Calculator.py")
    
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