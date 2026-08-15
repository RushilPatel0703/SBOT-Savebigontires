// Simple Mobile Filter JavaScript
// Intelligent Type, Size, and Brand dropdowns for tire collections

(function() {
  'use strict';
  
  console.log('🚀 Simple Mobile Filter script is loading...');
  
  // Only initialize if not already done
  if (window.simpleMobileFilterInitialized) {
    console.log('Simple Mobile Filter already initialized, skipping');
    return;
  }
  window.simpleMobileFilterInitialized = true;
  console.log('🚀 Simple Mobile Filter initializing for the first time');

  function applyMobileFilter() {
    // Check if we're on a wheels collection by looking for wheel-specific dropdowns
    const wheelDiameterSelect = document.getElementById('mobile-wheeldiameter-filter');
    const boltPatternSelect = document.getElementById('mobile-boltpattern-filter');
    const typeSelect = document.getElementById('mobile-type-filter');
    const sizeSelect = document.getElementById('mobile-size-filter');
    
    const isWheelsCollection = !!(wheelDiameterSelect && boltPatternSelect);
    
    let selectedTags = [];
    
    if (isWheelsCollection) {
      // For wheels collections, use WheelDiameter and BoltPattern
      if (wheelDiameterSelect && wheelDiameterSelect.value) {
        selectedTags.push(wheelDiameterSelect.value);
      }
      if (boltPatternSelect && boltPatternSelect.value) {
        selectedTags.push(boltPatternSelect.value);
      }
    } else {
      // For other collections, use Type, Size, and Brand
      if (typeSelect && typeSelect.value) {
        selectedTags.push(typeSelect.value);
      }
      if (sizeSelect && sizeSelect.value) {
        selectedTags.push(sizeSelect.value);
      }
      
      // Brand filter
      const brandSelect = document.getElementById('mobile-brand-filter');
      if (brandSelect && brandSelect.value) {
        selectedTags.push(brandSelect.value);
      }
    }
    
    // Build URL with query parameter format for consistency
    let basePath = window.location.pathname;
    const pathParts = basePath.split('/');
    if (pathParts.length > 3 && pathParts[1] === 'collections') {
      basePath = '/' + pathParts.slice(1, 3).join('/');
    }
    
    let newUrl = basePath;
    
    // Use traditional Shopify collection filtering with path-based tags
    if (selectedTags.length > 0) {
      // URL encode each tag properly
      const encodedTags = selectedTags.map(tag => encodeURIComponent(tag));
      newUrl += '/' + encodedTags.join('+');
    }
    
    // Preserve other query parameters except page
    const params = new URLSearchParams(window.location.search);
    params.delete('page'); // Reset to page 1 when filtering
    
    if (params.toString()) {
      newUrl += '?' + params.toString();
    }
    
    console.log('Navigating to:', newUrl);
    window.location.href = newUrl;
  }

  // Extract tags from URL and set dropdown selections
  function setDropdownSelectionsFromURL() {
    const path = window.location.pathname;
    const pathParts = path.split('/');
    const urlParams = new URLSearchParams(window.location.search);
    
    console.log('🔍 setDropdownSelectionsFromURL - Full URL:', window.location.href);
    console.log('🔍 setDropdownSelectionsFromURL - Path:', path);
    console.log('🔍 setDropdownSelectionsFromURL - Path parts:', pathParts);
    console.log('🔍 setDropdownSelectionsFromURL - URL params:', Object.fromEntries(urlParams));
    
    let tags = [];
    
    // Check for path-based filtering first (traditional Shopify format)
    if (pathParts.length > 3 && pathParts[1] === 'collections') {
      const tagString = pathParts[3];
      console.log('🔍 setDropdownSelectionsFromURL - Tag string from path:', tagString);
      if (tagString) {
        tags = tagString.split('+').map(tag => decodeURIComponent(tag));
      }
    }
    // Fallback to query parameter filtering (if path-based doesn't exist)
    else {
      const filterTags = urlParams.getAll('filter.p.tag');
      if (filterTags.length > 0) {
        tags = filterTags;
        console.log('🔍 setDropdownSelectionsFromURL - Found filter.p.tag values:', filterTags);
        console.log('🔍 setDropdownSelectionsFromURL - Decoded filter tags:', tags.map(tag => decodeURIComponent(tag)));
      }
    }
    
    console.log('🔍 setDropdownSelectionsFromURL - Final extracted tags:', tags);
    
    if (tags.length > 0) {
      const wheelDiameterSelect = document.getElementById('mobile-wheeldiameter-filter');
      const boltPatternSelect = document.getElementById('mobile-boltpattern-filter');
      const typeSelect = document.getElementById('mobile-type-filter');
      const sizeSelect = document.getElementById('mobile-size-filter');
      
      if (wheelDiameterSelect && boltPatternSelect) {
        // Wheels collection
        console.log('🔍 Processing tags for wheels collection');
        
        // Set WheelDiameter dropdown
        if (wheelDiameterSelect) {
          for (const tag of tags) {
            if (tag.startsWith('WheelDiameter_')) {
              console.log('Looking for WheelDiameter tag:', tag);
              for (let i = 0; i < wheelDiameterSelect.options.length; i++) {
                const option = wheelDiameterSelect.options[i];
                if (option.value === tag) {
                  console.log('Found matching WheelDiameter option, setting selected');
                  wheelDiameterSelect.selectedIndex = i;
                  option.selected = true;
                  break;
                }
              }
            }
          }
        }
        
        // Set BoltPattern dropdown
        if (boltPatternSelect) {
          for (const tag of tags) {
            // Check for both prefixed and unprefixed bolt pattern tags, including fallback patterns
            if (tag.startsWith('BoltPattern_') || (tag.includes('x') && /^\d+x\d+/.test(tag) && /^[3-9]x[5-9]\d/.test(tag))) {
              console.log('Looking for BoltPattern tag:', tag);
              for (let i = 0; i < boltPatternSelect.options.length; i++) {
                const option = boltPatternSelect.options[i];
                if (option.value === tag) {
                  console.log('Found matching BoltPattern option, setting selected');
                  boltPatternSelect.selectedIndex = i;
                  option.selected = true;
                  break;
                }
              }
            }
          }
        }
      } else {
        // Set Type dropdown
        if (typeSelect) {
          console.log('🔍 Type dropdown found with', typeSelect.options.length, 'options');
          for (let i = 0; i < typeSelect.options.length; i++) {
            console.log(`🔍   Option ${i}: value="${typeSelect.options[i].value}", text="${typeSelect.options[i].text}"`);
          }
          
          for (const tag of tags) {
            // Handle both 'Type_' and 'type_' formats (case insensitive)
            if (tag.startsWith('Type_') || tag.startsWith('type_')) {
              console.log('🔍 Looking for Type tag:', tag);
              
              // Try to find exact match first
              let found = false;
              for (let i = 0; i < typeSelect.options.length; i++) {
                const option = typeSelect.options[i];
                if (option.value === tag) {
                  console.log('Found exact matching Type option, setting selected');
                  typeSelect.selectedIndex = i;
                  option.selected = true;
                  found = true;
                  break;
                }
              }
              
              // If exact match not found, try to match by converting different formats
              if (!found) {
                let normalizedTag = tag;
                
                // Convert URL format (type_all-season) to tag format (Type_All Season)
                if (tag.startsWith('type_')) {
                  const urlValue = tag.substring(5); // Remove 'type_'
                  
                  // Handle specific mapping for tire types
                  const typeMapping = {
                    'all-season': 'All Season',
                    'all-weather': 'All Weather', 
                    'winter': 'Winter',
                    'summer': 'Summer',
                    'performance': 'Performance',
                    'all-terrain': 'All Terrain',
                    'mud-terrain': 'Mud Terrain'
                  };
                  
                  const tagValue = typeMapping[urlValue] || urlValue.replace(/-/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
                  normalizedTag = 'Type_' + tagValue;
                }
                // Handle case where tag has inconsistent capitalization (e.g., Type_all season -> Type_All Season)
                else if (tag.startsWith('Type_')) {
                  const tagValue = tag.substring(5); // Remove 'Type_'
                  const capitalizedValue = tagValue.replace(/\b\w/g, l => l.toUpperCase());
                  normalizedTag = 'Type_' + capitalizedValue;
                }
                
                // Try to match with normalized tag
                for (let i = 0; i < typeSelect.options.length; i++) {
                  const option = typeSelect.options[i];
                  if (option.value === normalizedTag) {
                    console.log('Found normalized matching Type option:', normalizedTag, 'setting selected');
                    typeSelect.selectedIndex = i;
                    option.selected = true;
                    found = true;
                    break;
                  }
                }
              }
            }
          }
        }
        
        // Set Size dropdown
        if (sizeSelect) {
          console.log('🔍 Size dropdown found with', sizeSelect.options.length, 'options');
          for (const tag of tags) {
            if (tag.startsWith('Size_')) {
              console.log('🔍 Looking for Size tag:', tag);
              for (let i = 0; i < sizeSelect.options.length; i++) {
                const option = sizeSelect.options[i];
                if (option.value === tag) {
                  console.log('Found matching Size option, setting selected');
                  sizeSelect.selectedIndex = i;
                  option.selected = true;
                  break;
                }
              }
            }
          }
        }
      }
    }
  }

  // Debug: Log current tags on page load
  function initDebugInfo() {
    console.log('Simple Mobile Filter initialized');
    console.log('Current URL:', window.location.href);
    console.log('Current pathname:', window.location.pathname);
    
    const wheelDiameterSelect = document.getElementById('mobile-wheeldiameter-filter');
    const boltPatternSelect = document.getElementById('mobile-boltpattern-filter');
    const typeSelect = document.getElementById('mobile-type-filter');
    const sizeSelect = document.getElementById('mobile-size-filter');
    
    const isWheelsCollection = !!(wheelDiameterSelect && boltPatternSelect);
    
    if (isWheelsCollection) {
      console.log('Wheels collection detected');
      if (wheelDiameterSelect) {
        console.log('WheelDiameter dropdown found with', wheelDiameterSelect.options.length, 'options');
      } else {
        console.log('WheelDiameter dropdown not found');
      }
      
      if (boltPatternSelect) {
        console.log('BoltPattern dropdown found with', boltPatternSelect.options.length, 'options');
      } else {
        console.log('BoltPattern dropdown not found');
      }
    } else {
      console.log('Non-wheels collection detected');
      if (typeSelect) {
        //console.log('Type dropdown found with', typeSelect.options.length, 'options');
        //console.log('Type selected value:', typeSelect.value);
        //console.log('Type selected index:', typeSelect.selectedIndex);
        
        // Log all options
        for (let i = 0; i < typeSelect.options.length; i++) {
          const option = typeSelect.options[i];
          //console.log(`  Option ${i}: value="${option.value}", text="${option.text}", selected=${option.selected}`);
        }
      } else {
        console.log('Type dropdown not found');
      }
      
      if (sizeSelect) {
        console.log('Size selected value:', sizeSelect.value);
      } else {
        console.log('Size dropdown not found');
      }
    }
    
    // Set selections from URL
    setDropdownSelectionsFromURL();
    
    // Log again after setting
    if (isWheelsCollection) {
      if (wheelDiameterSelect) {
        console.log('After URL selection - WheelDiameter selected value:', wheelDiameterSelect.value);
      }
      if (boltPatternSelect) {
        console.log('After URL selection - BoltPattern selected value:', boltPatternSelect.value);
      }
    } else {
      if (typeSelect) {
        console.log('After URL selection - Type selected value:', typeSelect.value);
        console.log('After URL selection - Type selected index:', typeSelect.selectedIndex);
      }
    }
  }

  // Make applyMobileFilter available globally
  window.applyMobileFilter = applyMobileFilter;

  // Initialize when DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initDebugInfo);
  } else {
    initDebugInfo();
  }

})();
