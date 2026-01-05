/**
 * UPlanet Gamification Library
 * 
 * A reusable JavaScript library for adding gamification elements to UPlanet applications.
 * Uses Bootstrap 5 for styling and vanilla JavaScript for logic.
 * 
 * Features:
 * - Onboarding Tutorial System
 * - Quest/Mission Board
 * - Achievement/Badge System
 * - Character Profile Card
 * - Search & Discovery
 * - Leaderboard
 * - Notification Center
 * - Skill Tree Visualization
 * 
 * @version 1.0.0
 * @license AGPL-3.0
 */

(function(global) {
    'use strict';

    // ============================================
    // CONSTANTS & CONFIGURATION
    // ============================================
    
    const STORAGE_PREFIX = 'uplanet_gamification_';
    const ANIMATION_DURATION = 300;
    
    const LEVEL_LABELS = {
        1: { label: 'Débutant', color: '#6c757d', icon: 'bi-star' },
        2: { label: 'Initié', color: '#17a2b8', icon: 'bi-star-half' },
        3: { label: 'Confirmé', color: '#28a745', icon: 'bi-star-fill' },
        4: { label: 'Avancé', color: '#ffc107', icon: 'bi-stars' },
        5: { label: 'Expert', color: '#fd7e14', icon: 'bi-award' },
        10: { label: 'Maître', color: '#dc3545', icon: 'bi-trophy' },
        50: { label: 'Grand Maître', color: '#6f42c1', icon: 'bi-gem' },
        100: { label: 'Maître Absolu', color: '#e83e8c', icon: 'bi-lightning-charge-fill' }
    };
    
    // Categories based on Chambre des Métiers et de l'Artisanat (CMA) + UPlanet extensions
    const SKILL_CATEGORIES = {
        // === CHAMBRE DES MÉTIERS - 4 GRANDES FAMILLES ===
        alimentation: { 
            name: 'Alimentation', 
            icon: 'bi-egg-fried', 
            color: '#f59e0b',
            description: 'Métiers de bouche : boulanger, pâtissier, boucher, chocolatier...',
            keywords: ['boulang', 'patissi', 'boucher', 'charcuti', 'traiteur', 'chocolat', 'glacier', 'poisson', 'fromag', 'brasseu', 'cuisine', 'chef', 'restau', 'sommelier', 'oenolog']
        },
        batiment: { 
            name: 'Bâtiment', 
            icon: 'bi-building', 
            color: '#6b7280',
            description: 'Construction : maçon, plombier, électricien, menuisier...',
            keywords: ['macon', 'maçon', 'plombi', 'electrici', 'menuisi', 'charpent', 'couvreur', 'peintre', 'carrel', 'platri', 'chauffag', 'construct', 'renov', 'ferronnier', 'serrurier']
        },
        fabrication: { 
            name: 'Fabrication', 
            icon: 'bi-tools', 
            color: '#8b5cf6',
            description: 'Production artisanale : ébéniste, bijoutier, potier, forgeron...',
            keywords: ['ebenist', 'ébénist', 'bijout', 'horlog', 'tapiss', 'forgeron', 'potier', 'poterie', 'ceramist', 'verrier', 'relieur', 'graveur', 'luthier', 'coutelier', 'coutur', 'tailleur']
        },
        services: { 
            name: 'Services', 
            icon: 'bi-scissors', 
            color: '#ec4899',
            description: 'Services : coiffeur, esthéticien, fleuriste, photographe...',
            keywords: ['coiffeur', 'coiffure', 'esthetic', 'fleurist', 'photograph', 'pressing', 'cordonnerie', 'taxi', 'ambulanci', 'répar', 'mécani', 'paysagist']
        },
        
        // === EXTENSIONS UPLANET ===
        numerique: { 
            name: 'Numérique', 
            icon: 'bi-laptop', 
            color: '#3b82f6',
            description: 'Technologies : développeur, designer, cybersécurité, IA...',
            keywords: ['program', 'develop', 'code', 'web', 'design', 'ux', 'ui', 'devops', 'cloud', 'cybers', 'ia', 'blockchain', 'crypto', 'robot', 'drone', '3d']
        },
        nature: { 
            name: 'Nature', 
            icon: 'bi-tree-fill', 
            color: '#22c55e',
            description: 'Agriculture : permaculture, apiculture, maraîchage...',
            keywords: ['permacultur', 'apicult', 'maraich', 'sylvicult', 'herborist', 'elevage', 'vigneron', 'agricult', 'paysan', 'fermier', 'compost', 'ecolog', 'botani']
        },
        art: { 
            name: 'Art', 
            icon: 'bi-palette-fill', 
            color: '#f43f5e',
            description: 'Arts : peinture, musique, danse, théâtre, sculpture...',
            keywords: ['peinture', 'peintre', 'dessin', 'illustrat', 'sculpt', 'musique', 'musicien', 'chant', 'danse', 'théâtre', 'theatre', 'cirque', 'calligraph', 'graffiti', 'tattoo']
        },
        sport: { 
            name: 'Sport', 
            icon: 'bi-trophy-fill', 
            color: '#eab308',
            description: 'Sport : natation, escalade, arts martiaux, yoga...',
            keywords: ['natation', 'nager', 'sport', 'fitness', 'escalade', 'martial', 'boxe', 'judo', 'karate', 'yoga', 'pilates', 'course', 'running', 'cyclisme', 'surf', 'voile', 'plongée', 'ski', 'equitation']
        },
        sante: { 
            name: 'Santé', 
            icon: 'bi-heart-pulse-fill', 
            color: '#ef4444',
            description: 'Bien-être : massage, naturopathie, premiers secours...',
            keywords: ['secour', 'massage', 'kiné', 'ostéo', 'naturo', 'aromathérap', 'acupunct', 'shiatsu', 'reiki', 'méditation', 'sophrolog', 'diétét', 'nutritio']
        },
        education: { 
            name: 'Éducation', 
            icon: 'bi-mortarboard-fill', 
            color: '#14b8a6',
            description: 'Transmission : langues, pédagogie, formation, animation...',
            keywords: ['langue', 'english', 'français', 'pédagog', 'enseignant', 'formateur', 'animation', 'animateur', 'coach', 'mentor', 'éducat', 'soutien', 'orthophon']
        },
        other: { 
            name: 'Autres', 
            icon: 'bi-tag', 
            color: '#64748b',
            description: 'Autres savoir-faire',
            keywords: []
        }
    };

    // ============================================
    // UTILITY FUNCTIONS
    // ============================================
    
    function getStorage(key, defaultValue = null) {
        try {
            const value = localStorage.getItem(STORAGE_PREFIX + key);
            return value ? JSON.parse(value) : defaultValue;
        } catch (e) {
            console.warn('[Gamification] Storage read error:', e);
            return defaultValue;
        }
    }
    
    function setStorage(key, value) {
        try {
            localStorage.setItem(STORAGE_PREFIX + key, JSON.stringify(value));
        } catch (e) {
            console.warn('[Gamification] Storage write error:', e);
        }
    }
    
    function formatTimeAgo(date) {
        const seconds = Math.floor((new Date() - new Date(date)) / 1000);
        const intervals = [
            { label: 'an', seconds: 31536000 },
            { label: 'mois', seconds: 2592000 },
            { label: 'jour', seconds: 86400 },
            { label: 'heure', seconds: 3600 },
            { label: 'minute', seconds: 60 }
        ];
        
        for (const interval of intervals) {
            const count = Math.floor(seconds / interval.seconds);
            if (count >= 1) {
                return `il y a ${count} ${interval.label}${count > 1 && interval.label !== 'mois' ? 's' : ''}`;
            }
        }
        return 'à l\'instant';
    }
    
    function getLevelInfo(level) {
        const numLevel = parseInt(level) || 1;
        let info = LEVEL_LABELS[1];
        
        for (const [threshold, data] of Object.entries(LEVEL_LABELS).sort((a, b) => b[0] - a[0])) {
            if (numLevel >= parseInt(threshold)) {
                info = data;
                break;
            }
        }
        
        return { ...info, level: numLevel };
    }
    
    function truncateAddress(address, length = 8) {
        if (!address) return '...';
        if (address.length <= length * 2) return address;
        return `${address.substring(0, length)}...${address.substring(address.length - length)}`;
    }
    
    /**
     * Guess the category of a skill/mastery based on its text content
     * Uses Chambre des Métiers et de l'Artisanat (CMA) categories + UPlanet extensions
     * 
     * @param {string} text - Text to analyze (name, description, id)
     * @returns {string} - Category ID
     */
    function guessCategory(text) {
        if (!text) return 'other';
        const lowerText = text.toLowerCase();
        
        for (const [categoryId, categoryData] of Object.entries(SKILL_CATEGORIES)) {
            if (categoryId === 'other') continue;
            
            for (const keyword of categoryData.keywords) {
                if (lowerText.includes(keyword)) {
                    return categoryId;
                }
            }
        }
        
        return 'other';
    }
    
    /**
     * Get category information by ID
     * 
     * @param {string} categoryId - Category ID
     * @returns {Object} - Category info with name, icon, color, description
     */
    function getCategoryInfo(categoryId) {
        return SKILL_CATEGORIES[categoryId] || SKILL_CATEGORIES.other;
    }
    
    /**
     * Get all available categories
     * 
     * @returns {Object} - All skill categories
     */
    function getAllCategories() {
        return SKILL_CATEGORIES;
    }

    // ============================================
    // ONBOARDING TUTORIAL SYSTEM
    // ============================================
    
    class OnboardingTutorial {
        constructor(options = {}) {
            this.steps = options.steps || [];
            this.containerId = options.containerId || 'onboarding-container';
            this.storageKey = options.storageKey || 'onboarding_completed';
            this.onComplete = options.onComplete || (() => {});
            this.currentStep = 0;
            this.isCompleted = getStorage(this.storageKey, false);
        }
        
        shouldShow() {
            return !this.isCompleted && this.steps.length > 0;
        }
        
        start() {
            if (!this.shouldShow()) {
                console.log('[Onboarding] Already completed or no steps');
                return;
            }
            
            this.currentStep = 0;
            this.render();
        }
        
        skip() {
            this.complete();
        }
        
        complete() {
            this.isCompleted = true;
            setStorage(this.storageKey, true);
            this.hide();
            this.onComplete();
        }
        
        reset() {
            this.isCompleted = false;
            setStorage(this.storageKey, false);
            this.currentStep = 0;
        }
        
        nextStep() {
            if (this.currentStep < this.steps.length - 1) {
                this.currentStep++;
                this.render();
            } else {
                this.complete();
            }
        }
        
        prevStep() {
            if (this.currentStep > 0) {
                this.currentStep--;
                this.render();
            }
        }
        
        hide() {
            const container = document.getElementById(this.containerId);
            if (container) {
                container.style.display = 'none';
            }
        }
        
        render() {
            let container = document.getElementById(this.containerId);
            
            if (!container) {
                container = document.createElement('div');
                container.id = this.containerId;
                document.body.appendChild(container);
            }
            
            const step = this.steps[this.currentStep];
            const progress = ((this.currentStep + 1) / this.steps.length) * 100;
            
            container.innerHTML = `
                <div class="onboarding-overlay" style="
                    position: fixed;
                    top: 0;
                    left: 0;
                    right: 0;
                    bottom: 0;
                    background: rgba(0, 0, 0, 0.85);
                    z-index: 10000;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    animation: fadeIn 0.3s ease;
                ">
                    <div class="onboarding-card" style="
                        background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
                        border: 2px solid rgba(14, 165, 233, 0.5);
                        border-radius: 20px;
                        padding: 2rem;
                        max-width: 500px;
                        width: 90%;
                        box-shadow: 0 20px 60px rgba(0, 0, 0, 0.5), 0 0 40px rgba(14, 165, 233, 0.2);
                        animation: slideUp 0.4s ease;
                    ">
                        <!-- Header with Quest Icon -->
                        <div class="text-center mb-4">
                            <div style="
                                width: 80px;
                                height: 80px;
                                margin: 0 auto 1rem;
                                background: linear-gradient(135deg, #0ea5e9, #06b6d4);
                                border-radius: 50%;
                                display: flex;
                                align-items: center;
                                justify-content: center;
                                box-shadow: 0 0 30px rgba(14, 165, 233, 0.5);
                                animation: pulse 2s infinite;
                            ">
                                <i class="bi ${step.icon || 'bi-compass'}" style="font-size: 2.5rem; color: white;"></i>
                            </div>
                            <h3 style="color: #0ea5e9; margin-bottom: 0.5rem; font-weight: 700;">
                                ${step.title}
                            </h3>
                            <p style="color: rgba(255, 255, 255, 0.6); font-size: 0.85rem;">
                                Étape ${this.currentStep + 1} sur ${this.steps.length}
                            </p>
                        </div>
                        
                        <!-- Progress Bar -->
                        <div class="progress mb-4" style="height: 8px; background: rgba(255, 255, 255, 0.1); border-radius: 4px;">
                            <div class="progress-bar" style="
                                width: ${progress}%;
                                background: linear-gradient(90deg, #0ea5e9, #06b6d4);
                                border-radius: 4px;
                                transition: width 0.3s ease;
                            "></div>
                        </div>
                        
                        <!-- Content -->
                        <div style="color: rgba(255, 255, 255, 0.9); font-size: 1rem; line-height: 1.7; margin-bottom: 2rem;">
                            ${step.content}
                        </div>
                        
                        <!-- Action Buttons -->
                        <div class="d-flex justify-content-between align-items-center">
                            <button class="btn btn-outline-secondary" onclick="window.uplanetGamification.onboarding.skip()" style="
                                border-color: rgba(255, 255, 255, 0.3);
                                color: rgba(255, 255, 255, 0.6);
                            ">
                                <i class="bi bi-x-lg"></i> Passer
                            </button>
                            
                            <div class="d-flex gap-2">
                                ${this.currentStep > 0 ? `
                                    <button class="btn btn-outline-info" onclick="window.uplanetGamification.onboarding.prevStep()">
                                        <i class="bi bi-arrow-left"></i>
                                    </button>
                                ` : ''}
                                
                                <button class="btn btn-info" onclick="window.uplanetGamification.onboarding.nextStep()" style="
                                    background: linear-gradient(135deg, #0ea5e9, #06b6d4);
                                    border: none;
                                    padding: 0.5rem 1.5rem;
                                ">
                                    ${this.currentStep < this.steps.length - 1 ? 
                                        '<i class="bi bi-arrow-right"></i> Suivant' : 
                                        '<i class="bi bi-check-lg"></i> Commencer!'
                                    }
                                </button>
                            </div>
                        </div>
                    </div>
                </div>
            `;
            
            container.style.display = 'block';
        }
    }

    // ============================================
    // QUEST/MISSION BOARD SYSTEM
    // ============================================
    
    class QuestBoard {
        constructor(options = {}) {
            this.containerId = options.containerId || 'quest-board';
            this.quests = options.quests || [];
            this.userProgress = options.userProgress || {};
            this.onQuestClick = options.onQuestClick || (() => {});
        }
        
        setQuests(quests) {
            this.quests = quests;
        }
        
        setProgress(progress) {
            this.userProgress = progress;
        }
        
        getQuestStatus(quest) {
            const progress = this.userProgress[quest.id] || 0;
            const target = quest.target || 1;
            
            if (progress >= target) return 'completed';
            if (progress > 0) return 'in_progress';
            return 'available';
        }
        
        render() {
            const container = document.getElementById(this.containerId);
            if (!container) return;
            
            const mainQuests = this.quests.filter(q => q.type === 'main');
            const sideQuests = this.quests.filter(q => q.type === 'side');
            const dailyQuests = this.quests.filter(q => q.type === 'daily');
            
            container.innerHTML = `
                <div class="quest-board-container">
                    <style>
                        .quest-card {
                            background: rgba(255, 255, 255, 0.95);
                            border-radius: 15px;
                            border-left: 4px solid;
                            padding: 1rem;
                            margin-bottom: 1rem;
                            transition: all 0.3s ease;
                            cursor: pointer;
                        }
                        .quest-card:hover {
                            transform: translateX(5px);
                            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.1);
                        }
                        .quest-card.main { border-left-color: #f59e0b; }
                        .quest-card.side { border-left-color: #0ea5e9; }
                        .quest-card.daily { border-left-color: #10b981; }
                        .quest-card.completed { 
                            opacity: 0.7;
                            border-left-color: #6b7280;
                        }
                        .quest-progress {
                            height: 6px;
                            background: #e5e7eb;
                            border-radius: 3px;
                            overflow: hidden;
                            margin-top: 0.5rem;
                        }
                        .quest-progress-bar {
                            height: 100%;
                            border-radius: 3px;
                            transition: width 0.5s ease;
                        }
                        .quest-progress-bar.main { background: linear-gradient(90deg, #f59e0b, #fbbf24); }
                        .quest-progress-bar.side { background: linear-gradient(90deg, #0ea5e9, #38bdf8); }
                        .quest-progress-bar.daily { background: linear-gradient(90deg, #10b981, #34d399); }
                        .quest-reward {
                            display: inline-flex;
                            align-items: center;
                            gap: 4px;
                            padding: 2px 8px;
                            background: rgba(251, 191, 36, 0.2);
                            color: #b45309;
                            border-radius: 12px;
                            font-size: 0.75rem;
                            font-weight: 600;
                        }
                    </style>
                    
                    <!-- Main Quests -->
                    ${mainQuests.length > 0 ? `
                        <div class="mb-4">
                            <h6 class="d-flex align-items-center gap-2 mb-3" style="color: #f59e0b;">
                                <i class="bi bi-flag-fill"></i> QUÊTE PRINCIPALE
                            </h6>
                            ${mainQuests.map(q => this.renderQuestCard(q)).join('')}
                        </div>
                    ` : ''}
                    
                    <!-- Side Quests -->
                    ${sideQuests.length > 0 ? `
                        <div class="mb-4">
                            <h6 class="d-flex align-items-center gap-2 mb-3" style="color: #0ea5e9;">
                                <i class="bi bi-signpost-2-fill"></i> QUÊTES SECONDAIRES
                            </h6>
                            ${sideQuests.map(q => this.renderQuestCard(q)).join('')}
                        </div>
                    ` : ''}
                    
                    <!-- Daily Quests -->
                    ${dailyQuests.length > 0 ? `
                        <div class="mb-4">
                            <h6 class="d-flex align-items-center gap-2 mb-3" style="color: #10b981;">
                                <i class="bi bi-calendar-check-fill"></i> DÉFIS QUOTIDIENS
                            </h6>
                            ${dailyQuests.map(q => this.renderQuestCard(q)).join('')}
                        </div>
                    ` : ''}
                    
                    ${this.quests.length === 0 ? `
                        <div class="text-center py-4" style="color: #6b7280;">
                            <i class="bi bi-inbox" style="font-size: 3rem;"></i>
                            <p class="mt-2">Aucune quête disponible pour le moment</p>
                        </div>
                    ` : ''}
                </div>
            `;
        }
        
        renderQuestCard(quest) {
            const status = this.getQuestStatus(quest);
            const progress = this.userProgress[quest.id] || 0;
            const target = quest.target || 1;
            const percent = Math.min((progress / target) * 100, 100);
            
            return `
                <div class="quest-card ${quest.type} ${status}" onclick="window.uplanetGamification.questBoard.onQuestClick('${quest.id}')">
                    <div class="d-flex justify-content-between align-items-start">
                        <div class="flex-grow-1">
                            <div class="d-flex align-items-center gap-2 mb-1">
                                ${status === 'completed' ? 
                                    '<i class="bi bi-check-circle-fill text-success"></i>' :
                                    status === 'in_progress' ? 
                                        '<i class="bi bi-circle-half text-warning"></i>' :
                                        '<i class="bi bi-circle text-secondary"></i>'
                                }
                                <strong>${quest.title}</strong>
                            </div>
                            <p class="mb-1 small text-muted">${quest.description}</p>
                            
                            ${status !== 'completed' ? `
                                <div class="quest-progress">
                                    <div class="quest-progress-bar ${quest.type}" style="width: ${percent}%"></div>
                                </div>
                                <small class="text-muted">${progress}/${target}</small>
                            ` : ''}
                        </div>
                        
                        ${quest.reward ? `
                            <span class="quest-reward">
                                <i class="bi bi-star-fill"></i> ${quest.reward}
                            </span>
                        ` : ''}
                    </div>
                </div>
            `;
        }
    }

    // ============================================
    // CHARACTER PROFILE CARD
    // ============================================
    
    class ProfileCard {
        constructor(options = {}) {
            this.containerId = options.containerId || 'profile-card';
            this.userData = options.userData || {};
            this.onAvatarClick = options.onAvatarClick || (() => {});
        }
        
        setUserData(data) {
            this.userData = data;
        }
        
        render() {
            const container = document.getElementById(this.containerId);
            if (!container) return;
            
            const user = this.userData;
            const levelInfo = getLevelInfo(user.level || 1);
            
            container.innerHTML = `
                <div class="profile-card-container" style="
                    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
                    border-radius: 20px;
                    padding: 1.5rem;
                    color: white;
                    box-shadow: 0 10px 40px rgba(0, 0, 0, 0.3);
                ">
                    <!-- Avatar & Basic Info -->
                    <div class="d-flex align-items-center gap-3 mb-3">
                        <div class="profile-avatar" onclick="window.uplanetGamification.profileCard.onAvatarClick()" style="
                            width: 70px;
                            height: 70px;
                            border-radius: 50%;
                            background: linear-gradient(135deg, ${levelInfo.color}, ${levelInfo.color}88);
                            display: flex;
                            align-items: center;
                            justify-content: center;
                            cursor: pointer;
                            box-shadow: 0 0 20px ${levelInfo.color}44;
                            transition: transform 0.3s ease;
                        ">
                            ${user.avatar ? 
                                `<img src="${user.avatar}" style="width: 100%; height: 100%; border-radius: 50%; object-fit: cover;">` :
                                `<i class="bi bi-person-fill" style="font-size: 2rem;"></i>`
                            }
                        </div>
                        
                        <div class="flex-grow-1">
                            <h5 class="mb-0" style="color: white;">${user.name || 'Voyageur'}</h5>
                            <small style="color: rgba(255, 255, 255, 0.6);">
                                ${truncateAddress(user.npub || user.pubkey, 8)}
                            </small>
                            <div class="mt-1">
                                <span class="badge" style="background: ${levelInfo.color};">
                                    <i class="bi ${levelInfo.icon}"></i> ${levelInfo.label}
                                </span>
                            </div>
                        </div>
                    </div>
                    
                    <!-- Stats Grid -->
                    <div class="row g-2 mb-3">
                        <div class="col-4">
                            <div class="text-center p-2" style="background: rgba(255, 255, 255, 0.1); border-radius: 10px;">
                                <div style="font-size: 1.5rem; font-weight: 700; color: #0ea5e9;">
                                    ${user.masteries || 0}
                                </div>
                                <small style="color: rgba(255, 255, 255, 0.6);">Maîtrises</small>
                            </div>
                        </div>
                        <div class="col-4">
                            <div class="text-center p-2" style="background: rgba(255, 255, 255, 0.1); border-radius: 10px;">
                                <div style="font-size: 1.5rem; font-weight: 700; color: #10b981;">
                                    ${user.attestationsGiven || 0}
                                </div>
                                <small style="color: rgba(255, 255, 255, 0.6);">Validations</small>
                            </div>
                        </div>
                        <div class="col-4">
                            <div class="text-center p-2" style="background: rgba(255, 255, 255, 0.1); border-radius: 10px;">
                                <div style="font-size: 1.5rem; font-weight: 700; color: #f59e0b;">
                                    ${user.attestationsReceived || 0}
                                </div>
                                <small style="color: rgba(255, 255, 255, 0.6);">Reçues</small>
                            </div>
                        </div>
                    </div>
                    
                    <!-- Skills/Competencies -->
                    ${user.competencies && user.competencies.length > 0 ? `
                        <div class="mb-3">
                            <small style="color: rgba(255, 255, 255, 0.6);">
                                <i class="bi bi-lightning-fill"></i> Compétences
                            </small>
                            <div class="d-flex flex-wrap gap-1 mt-1">
                                ${user.competencies.slice(0, 5).map(c => `
                                    <span class="badge" style="background: rgba(14, 165, 233, 0.3); color: #7dd3fc;">
                                        ${c}
                                    </span>
                                `).join('')}
                                ${user.competencies.length > 5 ? `
                                    <span class="badge" style="background: rgba(255, 255, 255, 0.1);">
                                        +${user.competencies.length - 5}
                                    </span>
                                ` : ''}
                            </div>
                        </div>
                    ` : ''}
                    
                    <!-- XP Progress -->
                    ${user.xp !== undefined ? `
                        <div>
                            <div class="d-flex justify-content-between mb-1">
                                <small style="color: rgba(255, 255, 255, 0.6);">Expérience</small>
                                <small style="color: rgba(255, 255, 255, 0.6);">${user.xp}/${user.xpToNextLevel || 100} XP</small>
                            </div>
                            <div class="progress" style="height: 8px; background: rgba(255, 255, 255, 0.1);">
                                <div class="progress-bar" style="
                                    width: ${(user.xp / (user.xpToNextLevel || 100)) * 100}%;
                                    background: linear-gradient(90deg, #f59e0b, #fbbf24);
                                "></div>
                            </div>
                        </div>
                    ` : ''}
                </div>
            `;
        }
    }

    // ============================================
    // SEARCH & DISCOVERY CENTER
    // ============================================
    
    class DiscoveryCenter {
        constructor(options = {}) {
            this.containerId = options.containerId || 'discovery-center';
            this.items = options.items || [];
            this.categories = options.categories || [];
            this.onItemClick = options.onItemClick || (() => {});
            this.onSearch = options.onSearch || (() => {});
            this.searchQuery = '';
            this.selectedCategory = null;
        }
        
        setItems(items) {
            this.items = items;
        }
        
        setCategories(categories) {
            this.categories = categories;
        }
        
        search(query) {
            this.searchQuery = query.toLowerCase();
            this.render();
        }
        
        filterByCategory(category) {
            this.selectedCategory = category;
            this.render();
        }
        
        getFilteredItems() {
            let filtered = [...this.items];
            
            if (this.searchQuery) {
                filtered = filtered.filter(item => 
                    item.name.toLowerCase().includes(this.searchQuery) ||
                    (item.description && item.description.toLowerCase().includes(this.searchQuery))
                );
            }
            
            if (this.selectedCategory) {
                filtered = filtered.filter(item => item.category === this.selectedCategory);
            }
            
            return filtered;
        }
        
        render() {
            const container = document.getElementById(this.containerId);
            if (!container) return;
            
            const filtered = this.getFilteredItems();
            const popular = [...this.items].sort((a, b) => (b.holders || 0) - (a.holders || 0)).slice(0, 3);
            const newest = [...this.items].sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0)).slice(0, 3);
            
            container.innerHTML = `
                <div class="discovery-container">
                    <!-- Search Bar -->
                    <div class="mb-4">
                        <div class="input-group" style="max-width: 500px;">
                            <span class="input-group-text" style="background: white; border-right: none;">
                                <i class="bi bi-search text-muted"></i>
                            </span>
                            <input type="text" class="form-control" 
                                placeholder="Rechercher une maîtrise..." 
                                value="${this.searchQuery}"
                                oninput="window.uplanetGamification.discovery.search(this.value)"
                                style="border-left: none;">
                        </div>
                    </div>
                    
                    <!-- Categories -->
                    ${this.categories.length > 0 ? `
                        <div class="mb-4">
                            <div class="d-flex flex-wrap gap-2">
                                <button class="btn btn-sm ${!this.selectedCategory ? 'btn-primary' : 'btn-outline-primary'}"
                                    onclick="window.uplanetGamification.discovery.filterByCategory(null)">
                                    Tous
                                </button>
                                ${this.categories.map(cat => `
                                    <button class="btn btn-sm ${this.selectedCategory === cat.id ? 'btn-primary' : 'btn-outline-primary'}"
                                        onclick="window.uplanetGamification.discovery.filterByCategory('${cat.id}')">
                                        <i class="bi ${cat.icon || 'bi-tag'}"></i> ${cat.name}
                                    </button>
                                `).join('')}
                            </div>
                        </div>
                    ` : ''}
                    
                    <!-- Quick Stats -->
                    ${!this.searchQuery && !this.selectedCategory ? `
                        <div class="row mb-4">
                            <div class="col-md-6 mb-3">
                                <div class="card h-100" style="border-left: 4px solid #f59e0b;">
                                    <div class="card-header bg-white">
                                        <h6 class="mb-0"><i class="bi bi-fire text-warning"></i> Populaires</h6>
                                    </div>
                                    <div class="card-body py-2">
                                        ${popular.map((item, i) => `
                                            <div class="d-flex align-items-center gap-2 py-1 ${i > 0 ? 'border-top' : ''}"
                                                style="cursor: pointer;"
                                                onclick="window.uplanetGamification.discovery.onItemClick('${item.id}')">
                                                <span class="badge bg-warning text-dark">#${i + 1}</span>
                                                <span class="flex-grow-1">${item.name}</span>
                                                <small class="text-muted">${item.holders || 0} maîtres</small>
                                            </div>
                                        `).join('')}
                                    </div>
                                </div>
                            </div>
                            <div class="col-md-6 mb-3">
                                <div class="card h-100" style="border-left: 4px solid #10b981;">
                                    <div class="card-header bg-white">
                                        <h6 class="mb-0"><i class="bi bi-stars text-success"></i> Nouvelles</h6>
                                    </div>
                                    <div class="card-body py-2">
                                        ${newest.map((item, i) => `
                                            <div class="d-flex align-items-center gap-2 py-1 ${i > 0 ? 'border-top' : ''}"
                                                style="cursor: pointer;"
                                                onclick="window.uplanetGamification.discovery.onItemClick('${item.id}')">
                                                <span class="badge bg-success">Nouveau</span>
                                                <span class="flex-grow-1">${item.name}</span>
                                                <small class="text-muted">${formatTimeAgo(item.created_at)}</small>
                                            </div>
                                        `).join('')}
                                    </div>
                                </div>
                            </div>
                        </div>
                    ` : ''}
                    
                    <!-- Results -->
                    <div class="row g-3">
                        ${filtered.length > 0 ? filtered.map(item => this.renderItemCard(item)).join('') : `
                            <div class="col-12 text-center py-4">
                                <i class="bi bi-search" style="font-size: 3rem; color: #cbd5e1;"></i>
                                <p class="text-muted mt-2">Aucun résultat trouvé</p>
                            </div>
                        `}
                    </div>
                </div>
            `;
        }
        
        renderItemCard(item) {
            const levelMatch = item.id?.match(/_X(\d+)$/);
            const level = levelMatch ? parseInt(levelMatch[1]) : null;
            
            return `
                <div class="col-md-6 col-lg-4">
                    <div class="card h-100" style="
                        border-radius: 15px;
                        border: none;
                        box-shadow: 0 4px 15px rgba(0, 0, 0, 0.1);
                        transition: all 0.3s ease;
                        cursor: pointer;
                    " onclick="window.uplanetGamification.discovery.onItemClick('${item.id}')"
                    onmouseover="this.style.transform='translateY(-5px)'"
                    onmouseout="this.style.transform='translateY(0)'">
                        <div class="card-body">
                            <div class="d-flex justify-content-between align-items-start mb-2">
                                <h6 class="mb-0">${item.name}</h6>
                                ${level ? `
                                    <span class="badge bg-primary">X${level}</span>
                                ` : ''}
                            </div>
                            <p class="text-muted small mb-3" style="
                                display: -webkit-box;
                                -webkit-line-clamp: 2;
                                -webkit-box-orient: vertical;
                                overflow: hidden;
                            ">${item.description || 'Aucune description'}</p>
                            
                            <div class="d-flex justify-content-between align-items-center">
                                <div class="d-flex gap-3">
                                    <span class="text-muted small">
                                        <i class="bi bi-people"></i> ${item.holders || 0}
                                    </span>
                                    <span class="text-muted small">
                                        <i class="bi bi-clock-history"></i> ${item.pending || 0}
                                    </span>
                                </div>
                                <span class="badge" style="background: rgba(14, 165, 233, 0.1); color: #0ea5e9;">
                                    ${item.min_attestations || 1} sig.
                                </span>
                            </div>
                        </div>
                    </div>
                </div>
            `;
        }
    }

    // ============================================
    // NOTIFICATION CENTER
    // ============================================
    
    class NotificationCenter {
        constructor(options = {}) {
            this.containerId = options.containerId || 'notification-center';
            this.notifications = [];
            this.maxNotifications = options.maxNotifications || 10;
            this.onNotificationClick = options.onNotificationClick || (() => {});
        }
        
        add(notification) {
            this.notifications.unshift({
                id: Date.now().toString(),
                timestamp: new Date().toISOString(),
                read: false,
                ...notification
            });
            
            // Keep only max notifications
            if (this.notifications.length > this.maxNotifications) {
                this.notifications = this.notifications.slice(0, this.maxNotifications);
            }
            
            this.render();
            this.showToast(notification);
        }
        
        markAsRead(id) {
            const notification = this.notifications.find(n => n.id === id);
            if (notification) {
                notification.read = true;
                this.render();
            }
        }
        
        markAllAsRead() {
            this.notifications.forEach(n => n.read = true);
            this.render();
        }
        
        clear() {
            this.notifications = [];
            this.render();
        }
        
        getUnreadCount() {
            return this.notifications.filter(n => !n.read).length;
        }
        
        showToast(notification) {
            // Create toast container if it doesn't exist
            let toastContainer = document.getElementById('toast-container');
            if (!toastContainer) {
                toastContainer = document.createElement('div');
                toastContainer.id = 'toast-container';
                toastContainer.style.cssText = `
                    position: fixed;
                    top: 100px;
                    right: 20px;
                    z-index: 9999;
                    max-width: 350px;
                `;
                document.body.appendChild(toastContainer);
            }
            
            const toastId = 'toast-' + Date.now();
            const iconMap = {
                success: 'bi-check-circle-fill text-success',
                error: 'bi-x-circle-fill text-danger',
                warning: 'bi-exclamation-circle-fill text-warning',
                info: 'bi-info-circle-fill text-info'
            };
            
            const toast = document.createElement('div');
            toast.id = toastId;
            toast.className = 'toast show';
            toast.setAttribute('role', 'alert');
            toast.style.cssText = `
                background: white;
                border-radius: 10px;
                box-shadow: 0 4px 20px rgba(0, 0, 0, 0.15);
                margin-bottom: 10px;
                animation: slideInRight 0.3s ease;
            `;
            
            toast.innerHTML = `
                <div class="toast-header" style="border-radius: 10px 10px 0 0;">
                    <i class="bi ${iconMap[notification.type] || iconMap.info} me-2"></i>
                    <strong class="me-auto">${notification.title || 'Notification'}</strong>
                    <small>${formatTimeAgo(new Date())}</small>
                    <button type="button" class="btn-close" onclick="document.getElementById('${toastId}').remove()"></button>
                </div>
                <div class="toast-body">
                    ${notification.message}
                </div>
            `;
            
            toastContainer.appendChild(toast);
            
            // Auto-remove after 5 seconds
            setTimeout(() => {
                toast.style.animation = 'slideOutRight 0.3s ease forwards';
                setTimeout(() => toast.remove(), 300);
            }, 5000);
        }
        
        render() {
            const container = document.getElementById(this.containerId);
            if (!container) return;
            
            const unreadCount = this.getUnreadCount();
            
            container.innerHTML = `
                <div class="notification-center-container">
                    <div class="d-flex justify-content-between align-items-center mb-3">
                        <h6 class="mb-0">
                            <i class="bi bi-bell-fill"></i> Notifications
                            ${unreadCount > 0 ? `<span class="badge bg-danger ms-2">${unreadCount}</span>` : ''}
                        </h6>
                        ${this.notifications.length > 0 ? `
                            <button class="btn btn-sm btn-outline-secondary" onclick="window.uplanetGamification.notifications.markAllAsRead()">
                                Tout marquer lu
                            </button>
                        ` : ''}
                    </div>
                    
                    <div class="notification-list">
                        ${this.notifications.length > 0 ? this.notifications.map(n => this.renderNotification(n)).join('') : `
                            <div class="text-center py-4 text-muted">
                                <i class="bi bi-bell-slash" style="font-size: 2rem;"></i>
                                <p class="mt-2 mb-0">Aucune notification</p>
                            </div>
                        `}
                    </div>
                </div>
            `;
        }
        
        renderNotification(notification) {
            const iconMap = {
                attestation: 'bi-check-circle-fill text-success',
                request: 'bi-person-plus-fill text-info',
                credential: 'bi-award-fill text-warning',
                level_up: 'bi-arrow-up-circle-fill text-primary',
                message: 'bi-chat-fill text-secondary'
            };
            
            return `
                <div class="notification-item d-flex gap-3 p-3 ${notification.read ? '' : 'bg-light'}" 
                    style="border-radius: 10px; margin-bottom: 0.5rem; cursor: pointer; transition: background 0.2s;"
                    onclick="window.uplanetGamification.notifications.onNotificationClick('${notification.id}')"
                    onmouseover="this.style.background='#f3f4f6'"
                    onmouseout="this.style.background='${notification.read ? 'transparent' : '#f3f4f6'}'">
                    <div class="flex-shrink-0">
                        <i class="bi ${iconMap[notification.type] || 'bi-bell'}" style="font-size: 1.5rem;"></i>
                    </div>
                    <div class="flex-grow-1">
                        <div class="d-flex justify-content-between">
                            <strong>${notification.title || 'Notification'}</strong>
                            <small class="text-muted">${formatTimeAgo(notification.timestamp)}</small>
                        </div>
                        <p class="mb-0 small text-muted">${notification.message}</p>
                    </div>
                    ${!notification.read ? '<div class="flex-shrink-0"><span class="badge bg-primary">Nouveau</span></div>' : ''}
                </div>
            `;
        }
    }

    // ============================================
    // SKILL TREE VISUALIZATION
    // ============================================
    
    class SkillTree {
        constructor(options = {}) {
            this.containerId = options.containerId || 'skill-tree';
            this.skills = options.skills || [];
            this.userSkills = options.userSkills || [];
            this.onSkillClick = options.onSkillClick || (() => {});
        }
        
        setSkills(skills) {
            this.skills = skills;
        }
        
        setUserSkills(userSkills) {
            this.userSkills = userSkills;
        }
        
        isUnlocked(skillId) {
            return this.userSkills.includes(skillId);
        }
        
        canUnlock(skill) {
            if (!skill.requires) return true;
            return skill.requires.every(req => this.isUnlocked(req));
        }
        
        render() {
            const container = document.getElementById(this.containerId);
            if (!container) return;
            
            // Group skills by level/tier
            const tiers = {};
            this.skills.forEach(skill => {
                const tier = skill.tier || 0;
                if (!tiers[tier]) tiers[tier] = [];
                tiers[tier].push(skill);
            });
            
            const sortedTiers = Object.keys(tiers).sort((a, b) => a - b);
            
            container.innerHTML = `
                <div class="skill-tree-container" style="
                    background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
                    border-radius: 20px;
                    padding: 2rem;
                    overflow-x: auto;
                ">
                    <style>
                        .skill-node {
                            width: 70px;
                            height: 70px;
                            border-radius: 50%;
                            display: flex;
                            align-items: center;
                            justify-content: center;
                            cursor: pointer;
                            transition: all 0.3s ease;
                            position: relative;
                        }
                        .skill-node.unlocked {
                            background: linear-gradient(135deg, #10b981, #059669);
                            box-shadow: 0 0 20px rgba(16, 185, 129, 0.5);
                        }
                        .skill-node.available {
                            background: linear-gradient(135deg, #3b82f6, #2563eb);
                            box-shadow: 0 0 20px rgba(59, 130, 246, 0.5);
                            animation: pulse 2s infinite;
                        }
                        .skill-node.locked {
                            background: rgba(255, 255, 255, 0.1);
                            opacity: 0.5;
                        }
                        .skill-node:hover {
                            transform: scale(1.1);
                        }
                        .skill-connector {
                            position: absolute;
                            background: rgba(255, 255, 255, 0.2);
                            z-index: 0;
                        }
                        .skill-connector.active {
                            background: linear-gradient(90deg, #10b981, #059669);
                        }
                        @keyframes pulse {
                            0%, 100% { box-shadow: 0 0 20px rgba(59, 130, 246, 0.5); }
                            50% { box-shadow: 0 0 30px rgba(59, 130, 246, 0.8); }
                        }
                    </style>
                    
                    <div class="d-flex flex-column align-items-center gap-5">
                        ${sortedTiers.reverse().map(tier => `
                            <div class="d-flex justify-content-center gap-4 flex-wrap">
                                ${tiers[tier].map(skill => this.renderSkillNode(skill)).join('')}
                            </div>
                        `).join('')}
                    </div>
                </div>
            `;
        }
        
        renderSkillNode(skill) {
            const isUnlocked = this.isUnlocked(skill.id);
            const canUnlock = this.canUnlock(skill);
            const status = isUnlocked ? 'unlocked' : canUnlock ? 'available' : 'locked';
            
            return `
                <div class="skill-node-wrapper text-center" style="position: relative;">
                    <div class="skill-node ${status}" 
                        onclick="window.uplanetGamification.skillTree.onSkillClick('${skill.id}')"
                        title="${skill.name}">
                        <i class="bi ${skill.icon || 'bi-star-fill'}" style="font-size: 1.5rem; color: white;"></i>
                    </div>
                    <div class="mt-2" style="color: ${isUnlocked ? '#10b981' : canUnlock ? '#3b82f6' : 'rgba(255,255,255,0.4)'}; font-size: 0.75rem; max-width: 80px;">
                        ${skill.name}
                    </div>
                </div>
            `;
        }
    }

    // ============================================
    // GLOBAL STYLES INJECTION
    // ============================================
    
    function injectStyles() {
        if (document.getElementById('uplanet-gamification-styles')) return;
        
        const style = document.createElement('style');
        style.id = 'uplanet-gamification-styles';
        style.textContent = `
            @keyframes fadeIn {
                from { opacity: 0; }
                to { opacity: 1; }
            }
            
            @keyframes slideUp {
                from { transform: translateY(20px); opacity: 0; }
                to { transform: translateY(0); opacity: 1; }
            }
            
            @keyframes slideInRight {
                from { transform: translateX(100%); opacity: 0; }
                to { transform: translateX(0); opacity: 1; }
            }
            
            @keyframes slideOutRight {
                from { transform: translateX(0); opacity: 1; }
                to { transform: translateX(100%); opacity: 0; }
            }
            
            @keyframes pulse {
                0%, 100% { transform: scale(1); }
                50% { transform: scale(1.05); }
            }
            
            @keyframes shimmer {
                0% { background-position: -200% 0; }
                100% { background-position: 200% 0; }
            }
            
            .uplanet-shimmer {
                background: linear-gradient(90deg, transparent, rgba(255,255,255,0.3), transparent);
                background-size: 200% 100%;
                animation: shimmer 1.5s infinite;
            }
        `;
        document.head.appendChild(style);
    }

    // ============================================
    // INITIALIZATION & EXPORT
    // ============================================
    
    function init() {
        injectStyles();
        console.log('[UPlanet Gamification] Library initialized');
    }

    // Create global API
    const UPlanetGamification = {
        // Classes
        OnboardingTutorial,
        QuestBoard,
        ProfileCard,
        DiscoveryCenter,
        NotificationCenter,
        SkillTree,
        
        // Utilities
        utils: {
            getStorage,
            setStorage,
            formatTimeAgo,
            getLevelInfo,
            truncateAddress,
            guessCategory,
            getCategoryInfo,
            getAllCategories
        },
        
        // Constants
        constants: {
            LEVEL_LABELS,
            SKILL_CATEGORIES
        },
        
        // Singleton instances (will be set by the app)
        onboarding: null,
        questBoard: null,
        profileCard: null,
        discovery: null,
        notifications: null,
        skillTree: null,
        
        // Initialize
        init
    };

    // Auto-init on load
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

    // Export to global scope
    global.uplanetGamification = UPlanetGamification;
    global.UPlanetGamification = UPlanetGamification;

})(typeof window !== 'undefined' ? window : this);
