import streamlit as st
import pandas as pd
import numpy as np
from typing import Dict, List, Any
import re
from pathlib import Path
from dataclasses import dataclass

@dataclass
class CalculationResult:
    formula_name: str
    rows_calculated: int
    errors: List[str]
    success_rate: float

def safe_eval(expression: str, variables: Dict[str, Any]) -> Any:
    """Safely evaluate a mathematical expression with given variables"""
    try:
        # Replace variable names with their values
        eval_expr = expression
        
        # Sort by length descending to avoid partial replacements
        sorted_vars = sorted(variables.keys(), key=len, reverse=True)
        
        for var_name in sorted_vars:
            pattern = r'\b' + re.escape(var_name) + r'\b'
            value = variables[var_name]
            
            # Handle NaN/None values
            if pd.isna(value) or value is None:
                value = 0
            
            eval_expr = re.sub(pattern, str(value), eval_expr)
        
        # Handle common functions
        eval_expr = eval_expr.replace('MAX(', 'max(')
        eval_expr = eval_expr.replace('MIN(', 'min(')
        eval_expr = eval_expr.replace('ABS(', 'abs(')
        
        # Safe evaluation with limited builtins
        allowed_builtins = {
            'max': max, 
            'min': min, 
            'abs': abs, 
            'round': round,
            'int': int,
            'float': float
        }
        
        result = eval(eval_expr, {"__builtins__": allowed_builtins}, {})
        return result
    
    except Exception as e:
        return f"ERROR: {str(e)}"


def calculate_row(row: pd.Series, formula_expr: str, mappings: Dict) -> Any:
    """Calculate formula result for a single row"""
    
    # Build variable values from row data
    var_values = {}
    
    for var_name, mapping in mappings.items():
        if mapping.mapped_header and mapping.mapped_header in row.index:
            value = row[mapping.mapped_header]
            var_values[var_name] = value
    
    # Evaluate the formula
    result = safe_eval(formula_expr, var_values)
    return result


def run_calculations(df: pd.DataFrame, 
                     formulas: List[Dict], 
                     mappings: Dict,
                     output_columns: List[str]) -> tuple[pd.DataFrame, List[CalculationResult]]:
    """
    Run all formulas on the dataframe
    
    Args:
        df: Input dataframe
        formulas: List of formula dictionaries
        mappings: Variable to header mappings
        output_columns: List of output column names to populate
    
    Returns:
        (result_df, calculation_results)
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
            # Use formula name as column if no match
            output_col = formula_name
        
        # Ensure column exists
        if output_col not in result_df.columns:
            result_df[output_col] = np.nan
        
        # Calculate for each row
        for idx, row in result_df.iterrows():
            try:
                result = calculate_row(row, formula_expr, mappings)
                
                if isinstance(result, str) and result.startswith("ERROR"):
                    errors.append(f"Row {idx}: {result}")
                else:
                    result_df.at[idx, output_col] = result
                    success_count += 1
            
            except Exception as e:
                errors.append(f"Row {idx}: {str(e)}")
        
        success_rate = (success_count / len(result_df)) * 100 if len(result_df) > 0 else 0
        
        calculation_results.append(CalculationResult(
            formula_name=formula_name,
            rows_calculated=success_count,
            errors=errors[:10],  # Limit to first 10 errors
            success_rate=success_rate
        ))
    
    return result_df, calculation_results