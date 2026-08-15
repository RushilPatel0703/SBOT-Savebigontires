/*============================================================================
  SaveBigOnTires Collection Page JavaScript Enhancements
  Professional collection page functionality and UX improvements
==============================================================================*/

(function($) {
  'use strict';

  var SaveBigCollection = {

    /*============================================================================
      #Initialize Collection Page
    ==============================================================================*/
    init: function() {
      this.bindEvents();
      this.initializeFilters();
      this.initializeViewToggle();
      this.initializeQuickView();
      this.initializeLazyLoading();
      this.initializeMobileFilters();
      this.initializeProductHover();
      this.initializeAccessibility();
      this.initializeAnalytics();
    },

    /*============================================================================
      #Event Binding
    ==============================================================================*/
    bindEvents: function() {
      var self = this;

      // Filter toggle for mobile
      $(document).on('click', '.savebigontires-toggle-filters', function(e) {
        e.preventDefault();
        self.toggleMobileFilters();
      });

      // Quick view buttons
      $(document).on('click', '.quick-view-btn', function(e) {
        e.preventDefault();
        e.stopPropagation();
        var productId = $(this).data('product-id');
        self.openQuickView(productId);
      });

      // Filter links with AJAX
      $(document).on('click', '.savebigontires-filter-link', function(e) {
        e.preventDefault();
        var url = $(this).attr('href');
        self.loadFilteredResults(url, $(this));
      });

      // Smooth scrolling for filter interactions
      $(document).on('click', '.collection-filters a', function() {
        self.scrollToProducts();
      });

      // Enhanced search functionality
      $('.savebigontires-search-input').on('input', function() {
        self.handleSearch($(this).val());
      });

      // Keyboard navigation
      $(document).on('keydown', function(e) {
        self.handleKeyboardNavigation(e);
      });

      // Window resize handling
      $(window).on('resize', function() {
        self.handleResize();
      });

      // Scroll handling for progressive loading
      $(window).on('scroll', function() {
        self.handleScroll();
      });
    },

    /*============================================================================
      #Filter Functionality
    ==============================================================================*/
    initializeFilters: function() {
      var self = this;

      // Track active filters
      this.activeFilters = [];

      // Initialize filter accordion behavior
      $('.savebigontires-filter-section').each(function() {
        var $section = $(this);
        var $title = $section.find('.savebigontires-filter-section-title');
        var $content = $section.find('.savebigontires-filter-list');

        // Add collapse/expand functionality
        $title.on('click', function() {
          $content.slideToggle(300);
          $section.toggleClass('collapsed');
        });
      });

      // Track filter selections
      $('.savebigontires-filter-link').on('click', function() {
        var filterValue = $(this).text().trim();
        self.trackFilterUsage(filterValue);
      });

      // Clear all filters functionality
      if ($('.clear-filters').length === 0) {
        $('.collection-filters').prepend(
          '<div class="clear-filters-wrapper">' +
          '<button class="clear-filters btn-link">Clear All Filters</button>' +
          '</div>'
        );
      }

      $(document).on('click', '.clear-filters', function() {
        self.clearAllFilters();
      });
    },

    /*============================================================================
      #View Toggle Enhancement
    ==============================================================================*/
    initializeViewToggle: function() {
      var self = this;

      // Store current view preference
      var currentView = this.getCurrentView();
      localStorage.setItem('SaveBigPreferredView', currentView);

      // Enhanced view switching with loading states
      $('.savebigontires-view-toggle').on('click', function() {
        var view = $(this).data('view');
        var $button = $(this);

        if ($button.hasClass('savebigontires-view-toggle--active')) {
          return;
        }

        // Add loading state
        self.showViewSwitchLoading();

        // Store preference
        localStorage.setItem('SaveBigPreferredView', view);

        // Switch view with smooth transition
        setTimeout(function() {
          window.location = self.updateUrlParameter(window.location.href, 'view', view);
        }, 150);
      });
    },

    /*============================================================================
      #Quick View Functionality
    ==============================================================================*/
    initializeQuickView: function() {
      var self = this;

      // Create quick view modal if it doesn't exist
      if ($('#quickViewModal').length === 0) {
        $('body').append(
          '<div id="quickViewModal" class="savebigontires-modal" style="display: none;">' +
          '<div class="savebigontires-modal-overlay"></div>' +
          '<div class="savebigontires-modal-content">' +
          '<button class="savebigontires-modal-close">&times;</button>' +
          '<div class="savebigontires-modal-body">' +
          '<div class="quick-view-loading">Loading...</div>' +
          '</div>' +
          '</div>' +
          '</div>'
        );
      }

      // Close modal events
      $(document).on('click', '.savebigontires-modal-close, .savebigontires-modal-overlay', function() {
        self.closeQuickView();
      });

      // Escape key to close
      $(document).on('keyup', function(e) {
        if (e.keyCode === 27) {
          self.closeQuickView();
        }
      });
    },

    /*============================================================================
      #Mobile Filter Enhancement
    ==============================================================================*/
    initializeMobileFilters: function() {
      var self = this;

      // Create mobile filter overlay
      if ($('.mobile-filter-overlay').length === 0) {
        $('body').append('<div class="mobile-filter-overlay" style="display: none;"></div>');
      }

      // Mobile filter behavior
      $('.mobile-filter-overlay').on('click', function() {
        self.closeMobileFilters();
      });

      // Swipe gestures for mobile
      if (window.innerWidth <= 768) {
        this.initializeSwipeGestures();
      }
    },

    /*============================================================================
      #Product Hover Effects
    ==============================================================================*/
    initializeProductHover: function() {
      var self = this;

      $('.product-list-item-wrapper').each(function() {
        var $item = $(this);
        var $image = $item.find('.product-list-img');

        $item.on('mouseenter', function() {
          self.enhanceProductHover($item);
        });

        $item.on('mouseleave', function() {
          self.resetProductHover($item);
        });
      });
    },

    /*============================================================================
      #Lazy Loading Enhancement
    ==============================================================================*/
    initializeLazyLoading: function() {
      var self = this;

      // Intersection Observer for better lazy loading
      if ('IntersectionObserver' in window) {
        var imageObserver = new IntersectionObserver(function(entries, observer) {
          entries.forEach(function(entry) {
            if (entry.isIntersecting) {
              var img = entry.target;
              self.loadImage(img);
              observer.unobserve(img);
            }
          });
        });

        $('.lazyload').each(function() {
          imageObserver.observe(this);
        });
      }
    },

    /*============================================================================
      #Accessibility Enhancements
    ==============================================================================*/
    initializeAccessibility: function() {
      var self = this;

      // Add ARIA labels where needed
      $('.savebigontires-filter-link').each(function() {
        var $link = $(this);
        var filterText = $link.text().trim();
        $link.attr('aria-label', 'Filter by ' + filterText);
      });

      // Add keyboard navigation for filters
      $('.savebigontires-filter-link').on('keydown', function(e) {
        if (e.keyCode === 13 || e.keyCode === 32) {
          e.preventDefault();
          $(this).trigger('click');
        }
      });

      // Focus management for modals
      $(document).on('shown.bs.modal', '#quickViewModal', function() {
        $(this).find('.savebigontires-modal-close').focus();
      });

      // Announce filter changes to screen readers
      $('<div id="filterAnnouncements" aria-live="polite" class="sr-only"></div>').appendTo('body');
    },

    /*============================================================================
      #Analytics and Tracking
    ==============================================================================*/
    initializeAnalytics: function() {
      var self = this;

      // Track page views
      this.trackPageView();

      // Track filter usage
      $('.savebigontires-filter-link').on('click', function() {
        var filterCategory = $(this).closest('.savebigontires-filter-section').find('.savebigontires-filter-section-title').text().trim();
        var filterValue = $(this).text().trim();
        self.trackEvent('Filter Used', filterCategory, filterValue);
      });

      // Track view switches
      $('.savebigontires-view-toggle').on('click', function() {
        var view = $(this).data('view');
        self.trackEvent('View Changed', 'Collection', view);
      });

      // Track quick view usage
      $(document).on('click', '.quick-view-btn', function() {
        var productId = $(this).data('product-id');
        self.trackEvent('Quick View', 'Product', productId);
      });
    },

    /*============================================================================
      #Helper Methods
    ==============================================================================*/
    getCurrentView: function() {
      return window.location.href.indexOf('view=list') > -1 ? 'list' : 'grid';
    },

    updateUrlParameter: function(url, param, paramVal) {
      var newAdditionalURL = "";
      var tempArray = url.split("?");
      var baseURL = tempArray[0];
      var additionalURL = tempArray[1];
      var temp = "";
      if (additionalURL) {
        tempArray = additionalURL.split("&");
        for (var i = 0; i < tempArray.length; i++) {
          if (tempArray[i].split('=')[0] != param) {
            newAdditionalURL += temp + tempArray[i];
            temp = "&";
          }
        }
      }
      var rows_txt = temp + "" + param + "=" + paramVal;
      return baseURL + "?" + newAdditionalURL + rows_txt;
    },

    showViewSwitchLoading: function() {
      $('.savebigontires-view-toggle').addClass('loading');
      $('.grid-uniform').append('<div class="view-switch-overlay">Switching view...</div>');
    },

    toggleMobileFilters: function() {
      var $filters = $('.collection-filters');
      var $overlay = $('.mobile-filter-overlay');

      if ($filters.hasClass('mobile-active')) {
        this.closeMobileFilters();
      } else {
        $filters.addClass('mobile-active');
        $overlay.show();
        $('body').addClass('no-scroll');
      }
    },

    closeMobileFilters: function() {
      $('.collection-filters').removeClass('mobile-active');
      $('.mobile-filter-overlay').hide();
      $('body').removeClass('no-scroll');
    },

    openQuickView: function(productId) {
      var self = this;
      var $modal = $('#quickViewModal');

      $modal.show();
      $('body').addClass('no-scroll');

      // Load product data (placeholder - would need actual implementation)
      this.loadQuickViewContent(productId);
    },

    closeQuickView: function() {
      $('#quickViewModal').hide();
      $('body').removeClass('no-scroll');
    },

    loadQuickViewContent: function(productId) {
      // Placeholder for quick view content loading
      var $body = $('.savebigontires-modal-body');
      $body.html('<div class="quick-view-content"><p>Quick view content for product ' + productId + '</p></div>');
    },

    loadFilteredResults: function(url, $filterLink) {
      var self = this;

      // Add loading state
      $filterLink.addClass('loading');
      $('.grid-uniform').addClass('loading');

      // Update URL without page reload (if supported)
      if (history.pushState) {
        history.pushState(null, null, url);
      } else {
        window.location.href = url;
      }
    },

    scrollToProducts: function() {
      $('html, body').animate({
        scrollTop: $('.grid-uniform').offset().top - 100
      }, 500);
    },

    clearAllFilters: function() {
      var baseUrl = window.location.href.split('?')[0];
      var currentView = this.getCurrentView();
      var newUrl = baseUrl + (currentView === 'list' ? '?view=list' : '');
      window.location.href = newUrl;
    },

    enhanceProductHover: function($item) {
      $item.find('.product-list-actions').fadeIn(200);
    },

    resetProductHover: function($item) {
      $item.find('.product-list-actions').fadeOut(200);
    },

    loadImage: function(img) {
      var $img = $(img);
      var src = $img.data('src');
      if (src) {
        $img.attr('src', src).removeClass('lazyload');
      }
    },

    handleSearch: function(query) {
      // Debounced search functionality
      clearTimeout(this.searchTimeout);
      this.searchTimeout = setTimeout(function() {
        // Implement search logic here
        console.log('Searching for:', query);
      }, 300);
    },

    handleKeyboardNavigation: function(e) {
      // Implement keyboard shortcuts
      if (e.ctrlKey || e.metaKey) {
        switch(e.keyCode) {
          case 70: // Ctrl+F for filter focus
            e.preventDefault();
            $('.savebigontires-filter-link:first').focus();
            break;
        }
      }
    },

    handleResize: function() {
      // Handle responsive behavior
      if (window.innerWidth > 768) {
        this.closeMobileFilters();
      }
    },

    handleScroll: function() {
      // Progressive loading or infinite scroll could be implemented here
      var scrollTop = $(window).scrollTop();
      var windowHeight = $(window).height();
      var documentHeight = $(document).height();

      // If near bottom, could trigger pagination
      if (scrollTop + windowHeight > documentHeight - 1000) {
        // Trigger next page load
      }
    },

    initializeSwipeGestures: function() {
      var self = this;
      var startX = 0;
      var startY = 0;

      $('.collection-filters').on('touchstart', function(e) {
        startX = e.originalEvent.touches[0].clientX;
        startY = e.originalEvent.touches[0].clientY;
      });

      $('.collection-filters').on('touchend', function(e) {
        var endX = e.originalEvent.changedTouches[0].clientX;
        var endY = e.originalEvent.changedTouches[0].clientY;
        var diffX = startX - endX;
        var diffY = startY - endY;

        // Swipe left to close filters
        if (Math.abs(diffX) > Math.abs(diffY) && diffX > 50) {
          self.closeMobileFilters();
        }
      });
    },

    trackPageView: function() {
      if (typeof gtag !== 'undefined') {
        gtag('config', 'GA_MEASUREMENT_ID', {
          page_title: document.title,
          page_location: window.location.href
        });
      }
    },

    trackEvent: function(action, category, label, value) {
      if (typeof gtag !== 'undefined') {
        gtag('event', action, {
          event_category: category,
          event_label: label,
          value: value
        });
      }
    },

    trackFilterUsage: function(filterValue) {
      // Track filter usage for optimization
      var usage = JSON.parse(localStorage.getItem('filterUsage') || '{}');
      usage[filterValue] = (usage[filterValue] || 0) + 1;
      localStorage.setItem('filterUsage', JSON.stringify(usage));
    }
  };

  /*============================================================================
    #Additional CSS for JavaScript functionality
  ==============================================================================*/
  var additionalStyles = `
    <style>
    .mobile-filter-overlay {
      position: fixed;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      background-color: rgba(0, 0, 0, 0.5);
      z-index: 999;
    }

    .collection-filters.mobile-active {
      position: fixed;
      top: 0;
      left: 0;
      height: 100vh;
      width: 300px;
      z-index: 1000;
      overflow-y: auto;
      transform: translateX(0);
      transition: transform 0.3s ease;
    }

    .collection-filters {
      transform: translateX(-100%);
      transition: transform 0.3s ease;
    }

    .savebigontires-modal {
      position: fixed;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      z-index: 2000;
    }

    .savebigontires-modal-overlay {
      position: absolute;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      background-color: rgba(0, 0, 0, 0.7);
    }

    .savebigontires-modal-content {
      position: absolute;
      top: 50%;
      left: 50%;
      transform: translate(-50%, -50%);
      background-color: white;
      border-radius: 8px;
      max-width: 90%;
      max-height: 90%;
      overflow-y: auto;
    }

    .savebigontires-modal-close {
      position: absolute;
      top: 15px;
      right: 15px;
      background: none;
      border: none;
      font-size: 24px;
      cursor: pointer;
      color: #666;
      z-index: 1;
    }

    .quick-view-loading {
      padding: 40px;
      text-align: center;
    }

    .view-switch-overlay {
      position: absolute;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      background-color: rgba(255, 255, 255, 0.8);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 10;
    }

    .no-scroll {
      overflow: hidden;
    }

    .loading {
      opacity: 0.7;
      pointer-events: none;
    }

    .clear-filters-wrapper {
      padding: 20px;
      border-bottom: 1px solid #f0f0f0;
    }

    .clear-filters {
      color: #e31e24;
      text-decoration: none;
      font-weight: 600;
      padding: 8px 0;
      display: inline-block;
    }

    .clear-filters:hover {
      text-decoration: underline;
    }

    .sr-only {
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }

    @media (max-width: 768px) {
      .collection-filters {
        display: block;
      }

      .savebigontires-modal-content {
        max-width: 95%;
        max-height: 95%;
      }
    }
    </style>
  `;

  $('head').append(additionalStyles);

  /*============================================================================
    #Initialize on Document Ready
  ==============================================================================*/
  $(document).ready(function() {
    SaveBigCollection.init();
  });

})(jQuery);
