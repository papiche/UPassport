/**
 * Simple i18n Library for UPlanet Cookie Manager
 * Load and apply translations from JSON file
 */

class SimplI18n {
    constructor(translationsUrl, defaultLang = 'fr') {
        this.translationsUrl = translationsUrl;
        this.translations = {};
        this.currentLang = defaultLang;
        this.initialized = false;
    }

    /**
     * Load translations from JSON file
     */
    async load() {
        try {
            const response = await fetch(this.translationsUrl);
            this.translations = await response.json();
            this.initialized = true;
            
            // Check for saved language preference
            const savedLang = localStorage.getItem('preferred_language');
            if (savedLang && this.translations[savedLang]) {
                this.currentLang = savedLang;
            }
            
            return true;
        } catch (error) {
            console.error('Failed to load translations:', error);
            return false;
        }
    }

    /**
     * Get translation for a key
     */
    t(key, lang = null) {
        const targetLang = lang || this.currentLang;
        if (this.translations[targetLang] && this.translations[targetLang][key]) {
            return this.translations[targetLang][key];
        }
        // Fallback to English
        if (this.translations['en'] && this.translations['en'][key]) {
            return this.translations['en'][key];
        }
        // Return key if not found
        return key;
    }

    /**
     * Change current language
     */
    setLanguage(lang) {
        if (this.translations[lang]) {
            this.currentLang = lang;
            localStorage.setItem('preferred_language', lang);
            this.applyTranslations();
            return true;
        }
        return false;
    }

    /**
     * Apply translations to DOM elements with data-i18n attribute
     */
    applyTranslations() {
        if (!this.initialized) {
            console.warn('Translations not loaded yet');
            return;
        }

        // Update all elements with data-i18n attribute
        document.querySelectorAll('[data-i18n]').forEach(element => {
            const key = element.getAttribute('data-i18n');
            const translation = this.t(key);
            
            if (element.tagName === 'INPUT' || element.tagName === 'TEXTAREA') {
                element.placeholder = translation;
            } else {
                // Preserve existing emoji/icons at the start
                const emojiMatch = element.innerHTML.match(/^[\u{1F000}-\u{1F9FF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}]+\s*/u);
                const prefix = emojiMatch ? emojiMatch[0] : '';
                
                // Check if content contains HTML tags (like <strong>, <code>, etc.)
                if (translation.includes('<')) {
                    element.innerHTML = prefix + translation;
                } else {
                    element.textContent = prefix + translation;
                }
            }
        });

        // Update document title
        document.title = this.t('title');

        // Dispatch custom event for other scripts to react
        window.dispatchEvent(new CustomEvent('languageChanged', {
            detail: { language: this.currentLang }
        }));
    }

    /**
     * Setup language tabs
     */
    setupLanguageTabs(tabsSelector = '.language-tab') {
        const tabs = document.querySelectorAll(tabsSelector);
        
        tabs.forEach(tab => {
            // Set active tab based on current language
            if (tab.getAttribute('data-lang') === this.currentLang) {
                tab.classList.add('active');
            }
            
            // Add click handler
            tab.addEventListener('click', () => {
                tabs.forEach(t => t.classList.remove('active'));
                tab.classList.add('active');
                
                const lang = tab.getAttribute('data-lang');
                this.setLanguage(lang);
            });
        });
    }

    /**
     * Initialize i18n system
     */
    async init() {
        await this.load();
        this.applyTranslations();
        this.setupLanguageTabs();
    }

    /**
     * Get current language
     */
    getCurrentLanguage() {
        return this.currentLang;
    }

    /**
     * Get available languages
     */
    getAvailableLanguages() {
        return Object.keys(this.translations);
    }
}

// Export for use in other scripts
if (typeof module !== 'undefined' && module.exports) {
    module.exports = SimplI18n;
}

