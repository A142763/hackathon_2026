import os
from databricks import sql
from databricks.sdk.core import Config
import streamlit as st
import pandas as pd
from difflib import SequenceMatcher

# Ensure environment variable is set correctly
assert os.getenv('DATABRICKS_WAREHOUSE_ID'), "DATABRICKS_WAREHOUSE_ID must be set in app.yaml."

# Databricks config
cfg = Config()

# Query the SQL warehouse with Service Principal credentials
def sql_query_with_service_principal(query: str) -> pd.DataFrame:
    """Execute a SQL query and return the result as a pandas DataFrame."""
    with sql.connect(
        server_hostname=cfg.host,
        http_path=f"/sql/1.0/warehouses/{cfg.warehouse_id}",
        credentials_provider=lambda: cfg.authenticate  # Uses SP credentials from the environment variables
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(query)
            return cursor.fetchall_arrow().to_pandas()

# Query the SQL warehouse with the user credentials
def sql_query_with_user_token(query: str, user_token: str) -> pd.DataFrame:
    """Execute a SQL query and return the result as a pandas DataFrame."""
    with sql.connect(
        server_hostname=cfg.host,
        http_path=f"/sql/1.0/warehouses/{cfg.warehouse_id}",
        access_token=user_token  # Pass the user token into the SQL connect to query on behalf of user
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(query)
            return cursor.fetchall_arrow().to_pandas()

st.set_page_config(layout="wide")

st.header("Healthcare Facilities by Zip Code")

# Extract user access token from the request headers
user_token = st.context.headers.get('X-Forwarded-Access-Token')

# Get unique zip codes from facilities table
zip_query = """
    SELECT DISTINCT address_zipOrPostcode 
    FROM databricks_virtue_foundation_dataset_dais_2026.virtue_foundation_dataset.facilities 
    WHERE address_zipOrPostcode IS NOT NULL 
    ORDER BY address_zipOrPostcode
"""

zips_df = sql_query_with_user_token(zip_query, user_token=user_token)
zip_codes = zips_df['address_zipOrPostcode'].tolist()

# Get unique specialties from facilities table (parse JSON array)
specialties_query = """
    SELECT DISTINCT specialty
    FROM (
        SELECT EXPLODE(FROM_JSON(specialties, 'array<string>')) AS specialty
        FROM databricks_virtue_foundation_dataset_dais_2026.virtue_foundation_dataset.facilities
        WHERE specialties IS NOT NULL
    )
    ORDER BY specialty
"""

specialties_df = sql_query_with_user_token(specialties_query, user_token=user_token)
specialty_list = ['All'] + specialties_df['specialty'].tolist()

# Dropdowns for filters
st.subheader("Filter Facilities")
col1, col2 = st.columns(2)

with col1:
    selected_zip = st.selectbox(
        "Choose a zip code:",
        options=zip_codes,
        index=0 if len(zip_codes) > 0 else None
    )

with col2:
    selected_specialty = st.selectbox(
        "Choose a specialty:",
        options=specialty_list,
        index=0
    )

# Add free-form text field for "What do I need"
what_i_need = st.text_input(
    "What do I need:",
    placeholder="Enter text to fuzzy match against facility attributes"
)

# Query facilities for selected zip code and specialty
if selected_zip:
    # Base query
    facilities_query = f"""
        SELECT 
            name,
            organization_type,
            address_line1,
            address_city,
            address_stateOrRegion,
            address_zipOrPostcode,
            phone_numbers,
            email,
            officialWebsite,
            specialties
        FROM databricks_virtue_foundation_dataset_dais_2026.virtue_foundation_dataset.facilities
        WHERE address_zipOrPostcode = '{selected_zip}'
    """
    
    # Add specialty filter if not 'All'
    if selected_specialty != 'All':
        facilities_query += f"""
        AND EXISTS (
            SELECT 1
            FROM (SELECT EXPLODE(FROM_JSON(specialties, 'array<string>')) AS specialty) AS s
            WHERE s.specialty = '{selected_specialty}'
        )
        """
    
    facilities_query += " ORDER BY name"
    
    facilities_data = sql_query_with_user_token(facilities_query, user_token=user_token)
    
    # Display metrics
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Total Facilities", len(facilities_data))
    with col2:
        org_types = facilities_data['organization_type'].nunique() if 'organization_type' in facilities_data.columns else 0
        st.metric("Organization Types", org_types)
    with col3:
        if selected_specialty == 'All':
            st.metric("Filter", "All Specialties")
        else:
            st.metric("Specialty", selected_specialty)
    
    # Add fuzzy match columns if "What do I need" field has a value
    if what_i_need and what_i_need.strip():
        def fuzzy_match_score(text1, text2):
            """Calculate fuzzy match score between two strings (0-1)."""
            if pd.isna(text1) or pd.isna(text2) or not text1 or not text2:
                return 0.0
            # Convert to lowercase for case-insensitive matching
            str1 = str(text1).lower()
            str2 = str(text2).lower()
            # Check if either string contains the other
            if str2 in str1 or str1 in str2:
                return 1.0
            # Otherwise use sequence matcher
            return SequenceMatcher(None, str1, str2).ratio()
        
        def create_match_indicator(value, threshold=0.3):
            """Create a match indicator column based on fuzzy match score."""
            search_text = what_i_need.lower()
            if pd.isna(value):
                return "No data"
            
            value_str = str(value).lower()
            
            # For JSON arrays (like specialties), check each item
            if value_str.startswith('['):
                try:
                    import json
                    items = json.loads(str(value))
                    if isinstance(items, list):
                        scores = [fuzzy_match_score(search_text, str(item)) for item in items]
                        max_score = max(scores) if scores else 0.0
                        if max_score >= 0.7:
                            return f"✓ Strong ({max_score:.0%})"
                        elif max_score >= threshold:
                            return f"~ Partial ({max_score:.0%})"
                        else:
                            return f"✗ No match"
                except:
                    pass
            
            # For regular strings
            score = fuzzy_match_score(search_text, value_str)
            if score >= 0.7:
                return f"✓ Strong ({score:.0%})"
            elif score >= threshold:
                return f"~ Partial ({score:.0%})"
            else:
                return f"✗ No match"
        
        # Add match indicator columns for the four fields
        facilities_data['match_specialties'] = facilities_data['specialties'].apply(create_match_indicator)
        
        # Check if other columns exist and add match indicators
        if 'procedure' in facilities_data.columns:
            facilities_data['match_procedure'] = facilities_data['procedure'].apply(create_match_indicator)
        else:
            facilities_data['match_procedure'] = "Column not available"
        
        if 'equipment' in facilities_data.columns:
            facilities_data['match_equipment'] = facilities_data['equipment'].apply(create_match_indicator)
        else:
            facilities_data['match_equipment'] = "Column not available"
        
        if 'capability' in facilities_data.columns:
            facilities_data['match_capability'] = facilities_data['capability'].apply(create_match_indicator)
        else:
            facilities_data['match_capability'] = "Column not available"
    
    # Display facilities data
    filter_text = f"in {selected_zip}" + (f" with {selected_specialty}" if selected_specialty != 'All' else "")
    st.subheader(f"Facilities {filter_text}")
    st.dataframe(data=facilities_data, height=600, use_container_width=True)
else:
    st.info("No zip codes available in the database.")
