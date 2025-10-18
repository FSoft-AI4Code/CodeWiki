// CodeWiki GitHub Pages Viewer Application

(function() {
    'use strict';
    
    // Initialize marked
    marked.setOptions({
        gfm: true,
        breaks: true,
        highlight: function(code, lang) {
            if (lang && hljs.getLanguage(lang)) {
                return hljs.highlight(code, { language: lang }).value;
            }
            return hljs.highlightAuto(code).value;
        }
    });
    
    // Initialize mermaid
    mermaid.initialize({ startOnLoad: false, theme: 'default' });
    
    // Router
    class Router {
        constructor() {
            this.routes = {};
            this.currentRoute = null;
            window.addEventListener('hashchange', () => this.handleRoute());
        }
        
        handleRoute() {
            const hash = window.location.hash.slice(1) || '/';
            // Remove leading slash if present
            const cleanHash = hash.startsWith('/') ? hash.slice(1) : hash;
            const path = (hash === '/' || cleanHash === '') ? 'overview.md' : cleanHash;
            this.loadMarkdown(path);
            this.updateBreadcrumbs(path);
        }
        
        async loadMarkdown(filename) {
            const content = document.getElementById('markdown-content');
            
            // Show loading state
            content.innerHTML = '<div class="loading">Loading...</div>';
            
            try {
                const response = await fetch(filename);
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}: ${response.statusText}`);
                }
                const markdown = await response.text();
                this.renderMarkdown(markdown);
                this.updateActiveLink(filename);
            } catch (error) {
                console.error('Error loading markdown:', error);
                content.innerHTML = `
                    <h1>📄 Document Not Found</h1>
                    <p>Could not load <code>${filename}</code></p>
                    <p style="color: #6a737d; font-size: 0.9rem;">
                        ${error.message}
                    </p>
                    <p>
                        <a href="#/" style="color: #0366d6;">← Back to Overview</a>
                    </p>
                `;
            }
        }
        
        updateActiveLink(filename) {
            // Update active state on navigation links
            document.querySelectorAll('.nav-link').forEach(link => {
                link.classList.remove('active');
                const linkPath = link.getAttribute('href').replace('#/', '').replace('#', '');
                const currentPath = filename;
                if (linkPath === currentPath || (linkPath === '' && filename === 'overview.md')) {
                    link.classList.add('active');
                }
            });
        }
        
        renderMarkdown(markdown) {
            const html = marked.parse(markdown);
            const content = document.getElementById('markdown-content');
            content.innerHTML = html;
            
            // Process mermaid diagrams
            const mermaidBlocks = content.querySelectorAll('code.language-mermaid');
            mermaidBlocks.forEach((block, index) => {
                const div = document.createElement('div');
                div.className = 'mermaid';
                div.textContent = block.textContent;
                block.parentElement.replaceWith(div);
            });
            
            if (mermaidBlocks.length > 0) {
                mermaid.run();
            }
            
            // Intercept clicks on markdown links and convert to hash-based navigation
            this.interceptMarkdownLinks(content);
            
            // Scroll to top
            document.getElementById('content').scrollTop = 0;
        }
        
        interceptMarkdownLinks(container) {
            // Find all links within the markdown content
            const links = container.querySelectorAll('a[href]');
            
            links.forEach(link => {
                const href = link.getAttribute('href');
                
                // Only intercept links to .md files (internal documentation links)
                if (href && href.endsWith('.md') && !href.startsWith('http://') && !href.startsWith('https://')) {
                    link.addEventListener('click', (e) => {
                        e.preventDefault();
                        
                        // Remove leading './' or '/' if present
                        let cleanPath = href.replace(/^\.\//, '').replace(/^\//, '');
                        
                        // Update hash to trigger navigation
                        window.location.hash = `/${cleanPath}`;
                    });
                }
            });
        }
        
        updateBreadcrumbs(path) {
            const breadcrumbs = document.getElementById('breadcrumbs');
            
            // Handle overview case
            if (path === 'overview.md') {
                breadcrumbs.innerHTML = '<a href="#/">Home</a>';
                return;
            }
            
            const parts = path.split('/').filter(p => p);
            
            let html = '<a href="#/">Home</a>';
            let currentPath = '';
            
            for (const part of parts) {
                currentPath += (currentPath ? '/' : '') + part;
                html += ' <span class="separator">›</span> ';
                const displayName = formatModuleName(part.replace('.md', ''));
                html += `<a href="#/${currentPath}">${displayName}</a>`;
            }
            
            breadcrumbs.innerHTML = html;
        }
    }
    
    // Navigation tree renderer - improved to handle nested structures
    function renderNavTree(tree, container, depth = 0) {
        if (!tree || typeof tree !== 'object') return;
        
        for (const [moduleName, moduleData] of Object.entries(tree)) {
            // Skip if this is just metadata
            if (moduleName === 'description' || moduleName === 'components') continue;
            
            const item = document.createElement('div');
            item.className = 'nav-item';
            item.style.paddingLeft = `${depth * 1}rem`;
            
            const link = document.createElement('a');
            link.href = `#/${moduleName}.md`;
            link.textContent = formatModuleName(moduleName);
            link.className = 'nav-link';
            
            item.appendChild(link);
            container.appendChild(item);
            
            // Render children recursively if they exist
            if (moduleData && moduleData.children && Object.keys(moduleData.children).length > 0) {
                renderNavTree(moduleData.children, container, depth + 1);
            }
        }
    }
    
    // Format module name for display
    function formatModuleName(name) {
        // Replace underscores and hyphens with spaces, capitalize words
        return name
            .replace(/[_-]/g, ' ')
            .split(' ')
            .map(word => word.charAt(0).toUpperCase() + word.slice(1))
            .join(' ');
    }
    
    // Render metadata info box
    function renderMetadata(metadata) {
        if (!metadata || !metadata.generation_info) return null;
        
        const metadataBox = document.createElement('div');
        metadataBox.style.cssText = `
            margin: 20px 0;
            padding: 15px;
            background: #f8fafc;
            border-radius: 8px;
            border: 1px solid #e2e8f0;
            font-size: 11px;
            line-height: 1.6;
        `;
        
        const header = document.createElement('h4');
        header.textContent = 'GENERATION INFO';
        header.style.cssText = `
            margin: 0 0 10px 0;
            font-size: 11px;
            color: #64748b;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            font-weight: 600;
        `;
        metadataBox.appendChild(header);
        
        const info = metadata.generation_info;
        const contentDiv = document.createElement('div');
        contentDiv.style.color = '#475569';
        
        let html = '';
        if (info.main_model) {
            html += `<div style="margin-bottom: 4px;"><strong>Model:</strong> ${info.main_model}</div>`;
        }
        if (info.timestamp) {
            const date = new Date(info.timestamp);
            html += `<div style="margin-bottom: 4px;"><strong>Generated:</strong> ${date.toLocaleString()}</div>`;
        }
        if (info.commit_id) {
            html += `<div style="margin-bottom: 4px;"><strong>Commit:</strong> ${info.commit_id.substring(0, 8)}</div>`;
        }
        if (metadata.statistics && metadata.statistics.total_components) {
            html += `<div><strong>Components:</strong> ${metadata.statistics.total_components}</div>`;
        }
        
        contentDiv.innerHTML = html;
        metadataBox.appendChild(contentDiv);
        
        return metadataBox;
    }
    
    // Mobile menu toggle
    function initMobileMenu() {
        const sidebar = document.getElementById('sidebar');
        const toggleBtn = document.getElementById('mobile-menu-toggle');
        
        toggleBtn.addEventListener('click', () => {
            sidebar.classList.toggle('open');
        });
        
        // Close sidebar when clicking outside on mobile
        document.addEventListener('click', (e) => {
            if (window.innerWidth < 768 && 
                !sidebar.contains(e.target) && 
                e.target !== toggleBtn) {
                sidebar.classList.remove('open');
            }
        });
    }
    
    // Initialize application
    function init() {
        // Render navigation tree
        const navTree = document.getElementById('nav-tree');
        if (MODULE_TREE) {
            // Add overview link first
            const overviewItem = document.createElement('div');
            overviewItem.className = 'nav-item';
            const overviewLink = document.createElement('a');
            overviewLink.href = '#/';
            overviewLink.textContent = 'Overview';
            overviewLink.className = 'nav-link';
            overviewItem.appendChild(overviewLink);
            navTree.appendChild(overviewItem);
            
            // Add metadata info box if available
            if (CODEWIKI_CONFIG && CODEWIKI_CONFIG.metadata) {
                const metadataBox = renderMetadata(CODEWIKI_CONFIG.metadata);
                if (metadataBox) {
                    navTree.appendChild(metadataBox);
                }
            }
            
            // Add divider
            const divider = document.createElement('div');
            divider.style.borderTop = '1px solid #e1e4e8';
            divider.style.margin = '0.5rem 0';
            navTree.appendChild(divider);
            
            // Render module tree
            renderNavTree(MODULE_TREE, navTree);
        }
        
        // Initialize router
        const router = new Router();
        
        // Initialize mobile menu
        initMobileMenu();
        
        // Initial load
        router.handleRoute();
    }
    
    // Start application when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();

