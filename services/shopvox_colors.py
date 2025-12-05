"""
Catalog-specific color mappings and processors for ShopVox.
Each catalog has its own color list for color lookup during item processing.
"""

from services.sanmar_colors import colors as sanmar_color_list


# Catalog color lookup tables
CATALOG_COLORS = {
    "SanMar": sanmar_color_list,
    "S&S Activewear": [],  # TODO: add S&S color list when available
    "Custom": [],  # Custom catalog doesn't need a color list
}


def get_catalog_colors(catalog: str) -> list:
    """
    Get the color list for a specific catalog.
    
    Args:
        catalog: Catalog name (e.g., "SanMar", "S&S Activewear", "Custom")
    
    Returns:
        List of valid colors for the catalog, or empty list if catalog unknown or custom
    """
    return CATALOG_COLORS.get(catalog, [])


def is_valid_catalog_color(catalog: str, color: str) -> bool:
    """
    Check if a color is valid for a specific catalog.
    
    Args:
        catalog: Catalog name
        color: Color name to validate
    
    Returns:
        True if color is in the catalog's color list, False otherwise
    """
    color_list = get_catalog_colors(catalog)
    if not color_list:  # Custom or unknown catalog
        return True
    return color in color_list
