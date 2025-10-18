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
            const path = hash === '/' ? 'overview.md' : hash;
            this.loadMarkdown(path);
            this.updateBreadcrumbs(path);
        }
        
        async loadMarkdown(filename) {
            try {
                const response = await fetch(filename);
                if (!response.ok) throw new Error(`Failed to load ${filename}`);
                const markdown = await response.text();
                this.renderMarkdown(markdown);
            } catch (error) {
                console.error('Error loading markdown:', error);
                const content = document.getElementById('markdown-content');
                content.innerHTML = `<h1>Error</h1><p>Could not load ${filename}</p>`;
            }
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
            
            // Scroll to top
            document.getElementById('content').scrollTop = 0;
        }
        
        updateBreadcrumbs(path) {
            const breadcrumbs = document.getElementById('breadcrumbs');
            const parts = path.split('/').filter(p => p);
            
            let html = '<a href="#/">Home</a>';
            let currentPath = '';
            
            for (const part of parts) {
                currentPath += '/' + part;
                html += ' <span class="separator">›</span> ';
                html += `<a href="#${currentPath}">${part.replace('.md', '')}</a>`;
            }
            
            breadcrumbs.innerHTML = html;
        }
    }
    
    // Navigation tree renderer
    function renderNavTree(tree, container, depth = 0) {
        if (!tree || typeof tree !== 'object') return;
        
        for (const [moduleName, moduleData] of Object.entries(tree)) {
            const item = document.createElement('div');
            item.className = 'nav-item';
            item.style.paddingLeft = `${depth * 1}rem`;
            
            const link = document.createElement('a');
            link.href = `#/${moduleName}.md`;
            link.textContent = moduleName;
            link.className = 'nav-link';
            
            // Highlight active link
            link.addEventListener('click', function() {
                document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));
                this.classList.add('active');
            });
            
            item.appendChild(link);
            container.appendChild(item);
            
            // Render children recursively
            if (moduleData.children && Object.keys(moduleData.children).length > 0) {
                renderNavTree(moduleData.children, container, depth + 1);
            }
        }
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
            overviewLink.className = 'nav-link active';
            overviewItem.appendChild(overviewLink);
            navTree.appendChild(overviewItem);
            
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

