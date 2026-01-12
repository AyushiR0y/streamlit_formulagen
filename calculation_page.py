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
from fuzzywuzzy import fuzz
from difflib import SequenceMatcher
import openpyxl
from pathlib import Path
from sentence_transformers import SentenceTransformer
from scipy.spatial.distance import cosine

load_dotenv()

# Configuration
ALLOWED_EXCEL_EXTENSIONS = {'xlsx', 'xls', 'csv'}
MAX_FILE_SIZE = 16 * 1024 * 1024  # 16MB

# Azure OpenAI Configuration
AZURE_API_KEY = os.getenv('AZURE_OPENAI_API_KEY')
AZURE_ENDPOINT = os.getenv('AZURE_OPENAI_ENDPOINT')
DEPLOYMENT_NAME = os.getenv('AZURE_OPENAI_DEPLOYMENT_NAME')
AZURE_API_VERSION = os.getenv('AZURE_OPENAI_API_VERSION')
EMBEDDING_MODEL = os.getenv('AZURE_OPENAI_EMBEDDING_MODEL', 'text-embedding-ada-002')

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

# Initialize embedding model for vector similarity (fallback to local model)
@st.cache_resource
def load_embedding_model():
    """Load sentence transformer model for vector embeddings"""
    try:
        # Use a lightweight model for efficiency
        model = SentenceTransformer('all-MiniLM-L6-v2')
        return model
    except Exception as e:
        st.warning(f"Could not load local embedding model: {e}")
        return None

EMBEDDING_MODEL_LOCAL = load_embedding_model()

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
    
    def lexical_similarity(self, var: str, header: str) -> float:
        """Calculate lexical similarity using multiple methods"""
        var_norm = self.normalize_text(var)
        header_norm = self.normalize_text(header)
        
        # Exact match
        if var_norm == header_norm:
            return 1.0
        
        # Substring match
        if var_norm in header_norm or header_norm in var_norm:
            return 0.9
        
        # Token-based matching
        var_tokens = set(var_norm.split())
        header_tokens = set(header_norm.split())
        
        if var_tokens and header_tokens:
            intersection = len(var_tokens & header_tokens)
            union = len(var_tokens | header_tokens)
            jaccard = intersection / union if union > 0 else 0
            
            return jaccard * 0.85
        
        return 0.0
    
    def fuzzy_similarity(self, var: str, header: str) -> float:
        """Calculate fuzzy string similarity"""
        var_norm = self.normalize_text(var)
        header_norm = self.normalize_text(header)
        
        # Levenshtein-based ratio
        ratio = fuzz.ratio(var_norm, header_norm) / 100.0
        partial_ratio = fuzz.partial_ratio(var_norm, header_norm) / 100.0
        token_sort_ratio = fuzz.token_sort_ratio(var_norm, header_norm) / 100.0
        
        # Weighted average
        score = (ratio * 0.4 + partial_ratio * 0.3 + token_sort_ratio * 0.3)
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
            
            return min(score, 1.0), reason
            
        except Exception as e:
            st.warning(f"AI semantic matching failed: {e}")
            return 0.0, f"Error: {str(e)}"
    
    def find_best_match(self, variable: str, headers: List[str], use_ai: bool = True) -> Optional[VariableMapping]:
        """Find the best matching header for a variable"""
        best_score = 0.0
        best_header = None
        best_method = None
        
        for header in headers:
            # Lexical matching
            lex_score = self.lexical_similarity(variable, header)
            
            # Fuzzy matching
            fuzzy_score = self.fuzzy_similarity(variable, header)
            
            # Combined score (before AI)
            combined_score = max(lex_score, fuzzy_score)
            
            if combined_score > best_score:
                best_score = combined_score
                best_header = header
                best_method = "lexical" if lex_score > fuzzy_score else "fuzzy"
        
        # If we have a reasonable match, try AI for confirmation
        if best_header and use_ai and best_score < 0.95:
            ai_score, ai_reason = self.semantic_similarity_ai(variable, best_header)
            
            # If AI gives higher confidence, use it
            if ai_score > best_score:
                best_score = ai_score
                best_method = f"semantic_ai ({ai_reason[:50]}...)"
        
        if best_header and best_score >= CONFIDENCE_THRESHOLDS['low']:
            return VariableMapping(
                variable_name=variable,
                mapped_header=best_header,
                confidence_score=best_score,
                matching_method=best_method,
                is_verified=best_score >= CONFIDENCE_THRESHOLDS['high']
            )
        
        return None
    
    def match_all_variables(self, variables: List[str], headers: List[str], use_ai: bool = True) -> Dict[str, VariableMapping]:
        """Match all variables to headers"""
        mappings = {}
        
        for var in variables:
            mapping = self.find_best_match(var, headers, use_ai)
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
        
        return mappings


def extract_variables_from_formulas(formulas: List[Dict]) -> Set[str]:
    """Extract all unique variables from formula expressions"""
    variables = set()
    
    # Common variable patterns in formulas
    var_pattern = r'\b([A-Z][A-Z0-9_]*)\b'
    
    for formula in formulas:
        expr = formula.get('formula_expression', '')
        matches = re.findall(var_pattern, expr)
        variables.update(matches)
    
    # Filter out common operators and keywords
    operators = {'MAX', 'MIN', 'SUM', 'AVG', 'IF', 'THEN', 'ELSE', 'AND', 'OR', 'NOT'}
    variables = {v for v in variables if v not in operators}
    
    return variables


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
                expr = re.sub(pattern, mapping.mapped_header, expr)
        
        mapped_formulas.append({
            'formula_name': formula.get('formula_name', ''),
            'original_expression': formula.get('formula_expression', ''),
            'mapped_expression': expr
        })
    
    return mapped_formulas


def set_custom_css():
    """Apply the same CSS from the main app"""
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
        </style>
        """,
        unsafe_allow_html=True
    )


def main():
    st.set_page_config(
        page_title="Formula Calculation - Variable Mapping",
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
        if st.button("← Back to Extraction"):
            st.switch_page("app.py")
        return
    
    if 'excel_headers' not in st.session_state:
        st.session_state.excel_headers = []
    
    if 'variable_mappings' not in st.session_state:
        st.session_state.variable_mappings = {}
    
    if 'mapping_complete' not in st.session_state:
        st.session_state.mapping_complete = False
    
    if 'excel_df' not in st.session_state:
        st.session_state.excel_df = None
    
    # Extract variables from formulas
    all_variables = extract_variables_from_formulas(st.session_state.formulas)
    
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
                    
                    use_ai = st.checkbox(
                        "Use AI for semantic matching",
                        value=not MOCK_MODE,
                        help="Enable AI-powered semantic matching for better accuracy (requires API key)",
                        disabled=MOCK_MODE
                    )
                    
                    if st.button("🔗 Start Variable Mapping", type="primary"):
                        with st.spinner("Analyzing variables and matching with headers..."):
                            matcher = VariableHeaderMatcher()
                            mappings = matcher.match_all_variables(
                                list(all_variables),
                                headers,
                                use_ai=use_ai
                            )
                            st.session_state.variable_mappings = mappings
                            st.success(f"✅ Mapped {len([m for m in mappings.values() if m.mapped_header])} out of {len(all_variables)} variables")
    
    with col2:
        st.subheader("📋 Extracted Variables")
        st.markdown("These variables were identified in your formulas and need to be mapped to Excel headers.")
        
        if all_variables:
            var_df = pd.DataFrame({
                'Variable Name': sorted(list(all_variables)),
                'Type': ['Needs Mapping'] * len(all_variables)
            })
            st.dataframe(var_df, use_container_width=True, hide_index=True)
        else:
            st.info("No variables detected in formulas.")
    
    # Variable Mapping Section
    if st.session_state.variable_mappings:
        st.markdown("---")
        st.subheader("🔗 Variable to Header Mappings")
        st.markdown("Review and edit the automatically generated mappings. Low confidence matches are flagged for your review.")
        
        # Create editable mapping table
        st.markdown("#### Mapping Table")
        
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
                # Dropdown to select/change header
                current_index = 0
                if mapping.mapped_header in st.session_state.excel_headers:
                    current_index = st.session_state.excel_headers.index(mapping.mapped_header)
                
                new_header = st.selectbox(
                    "Header",
                    options=[""] + st.session_state.excel_headers,
                    index=current_index if mapping.mapped_header else 0,
                    key=f"header_{var_name}",
                    label_visibility="collapsed"
                )
                
                # Update mapping if changed
                if new_header != mapping.mapped_header:
                    mapping.mapped_header = new_header
                    mapping.confidence_score = 1.0 if new_header else 0.0
                    mapping.matching_method = "manual"
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
        
        # Proceed button
        st.markdown("---")
        col_btn1, col_btn2 = st.columns([1, 1])
        
        with col_btn1:
            if st.button("✅ Confirm Mappings & View Formulas", type="primary"):
                # Check if all variables are mapped
                unmapped = [v for v, m in st.session_state.variable_mappings.items() if not m.mapped_header]
                
                if unmapped:
                    st.warning(f"⚠️ {len(unmapped)} variables are still unmapped: {', '.join(unmapped[:5])}{'...' if len(unmapped) > 5 else ''}")
                else:
                    st.session_state.mapping_complete = True
                    st.success("✅ All mappings confirmed!")
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
        st.markdown("Here are your formulas with variables replaced by the mapped Excel headers.")
        
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
                st.info("Calculation engine coming soon!")
    
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