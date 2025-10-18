// CodeWiki GitHub Pages Viewer Application

(function() {
    'use strict';
    
    // Configure marked
    if (typeof marked !== 'undefined') {
        marked.setOptions({
            gfm: true,
            breaks: true
        });
    }
    
    // Initialize mermaid
    mermaid.initialize({
        startOnLoad: false,
        theme: 'default',
        themeVariables: {
            primaryColor: '#2563eb',
            primaryTextColor: '#334155',
            primaryBorderColor: '#e2e8f0',
            lineColor: '#64748b',
            sectionBkgColor: '#f8fafc',
            altSectionBkgColor: '#f1f5f9',
            gridColor: '#e2e8f0',
            secondaryColor: '#f1f5f9',
            tertiaryColor: '#f8fafc'
        },
        flowchart: {
            htmlLabels: true,
            curve: 'basis'
        },
        sequence: {
            diagramMarginX: 50,
            diagramMarginY: 10,
            actorMargin: 50,
            width: 150,
            height: 65,
            boxMargin: 10,
            boxTextMargin: 5,
            noteMargin: 10,
            messageMargin: 35,
            mirrorActors: true,
            bottomMarginAdj: 1,
            useMaxWidth: true,
            rightAngles: false,
            showSequenceNumbers: false
        }
    });
    
    // Router
    class Router {
        constructor() {
            this.routes = {};
            this.currentRoute = null;
            window.addEventListener('hashchange', () => this.handleRoute());
        }
        
        handleRoute() {
            const hash = window.location.hash.slice(1) || '/';
            const cleanHash = hash.startsWith('/') ? hash.slice(1) : hash;
            const path = (hash === '/' || cleanHash === '') ? 'overview.md' : cleanHash;
            this.loadMarkdown(path);
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
                    <p style="color: #64748b; font-size: 0.9rem;">
                        ${error.message}
                    </p>
                    <p>
                        <a href="#/">← Back to Overview</a>
                    </p>
                `;
            }
        }
        
        updateActiveLink(filename) {
            document.querySelectorAll('.nav-link').forEach(link => {
                link.classList.remove('active');
                const linkPath = link.getAttribute('href').replace('#/', '').replace('#', '');
                const currentPath = filename;
                if (linkPath === currentPath || (linkPath === '' && filename === 'overview.md')) {
                    link.classList.add('active');
                }
            });
        }
        
        async renderMarkdown(markdown) {
            const content = document.getElementById('markdown-content');
            
            // Parse markdown to HTML
            const html = marked.parse(markdown);
            content.innerHTML = html;
            
            // Convert mermaid code blocks to mermaid divs
            const mermaidBlocks = content.querySelectorAll('code.language-mermaid');
            mermaidBlocks.forEach((block) => {
                const div = document.createElement('div');
                div.className = 'mermaid';
                div.textContent = block.textContent;
                block.parentElement.replaceWith(div);
            });
            
            // Render mermaid diagrams
            const mermaidDivs = content.querySelectorAll('.mermaid');
            if (mermaidDivs.length > 0) {
                try {
                    await mermaid.run({
                        nodes: mermaidDivs
                    });
                } catch (e) {
                    console.error('Mermaid rendering error:', e);
                }
            }
            
            // Scroll to top
            window.scrollTo(0, 0);
        }
    }
    
    // Navigation tree renderer
    function renderNavTree(tree, container, depth = 0) {
        if (!tree || typeof tree !== 'object') return;
        
        for (const [moduleName, moduleData] of Object.entries(tree)) {
            // Skip metadata fields
            if (moduleName === 'description' || moduleName === 'components') continue;
            
            // Create navigation item
            const item = document.createElement('div');
            if (depth > 0) {
                item.className = 'nav-subsection';
            } else {
                item.className = 'nav-item';
            }
            
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
        `;
        
        const header = document.createElement('h4');
        header.textContent = 'GENERATION INFO';
        header.style.cssText = `
            margin: 0 0 10px 0;
            font-size: 12px;
            color: #64748b;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        `;
        metadataBox.appendChild(header);
        
        const info = metadata.generation_info;
        const contentDiv = document.createElement('div');
        contentDiv.style.cssText = 'font-size: 11px; color: #475569; line-height: 1.4;';
        
        let html = '';
        if (info.main_model) {
            html += `<div style="margin-bottom: 4px;"><strong>Model:</strong> ${info.main_model}</div>`;
        }
        if (info.timestamp) {
            const date = new Date(info.timestamp);
            html += `<div style="margin-bottom: 4px;"><strong>Generated:</strong> ${date.toLocaleString().substring(0, 16)}</div>`;
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
    
    // Initialize application
    function init() {
        const navTree = document.getElementById('nav-tree');
        
        if (MODULE_TREE) {
            // Add overview link
            const navSection = document.createElement('div');
            navSection.className = 'nav-section';
            
            const overviewItem = document.createElement('div');
            overviewItem.className = 'nav-item';
            const overviewLink = document.createElement('a');
            overviewLink.href = '#/';
            overviewLink.textContent = 'Overview';
            overviewLink.className = 'nav-link';
            overviewItem.appendChild(overviewLink);
            navSection.appendChild(overviewItem);
            navTree.appendChild(navSection);
            
            // Add metadata info box if available
            if (CODEWIKI_CONFIG && CODEWIKI_CONFIG.metadata) {
                const metadataBox = renderMetadata(CODEWIKI_CONFIG.metadata);
                if (metadataBox) {
                    document.getElementById('metadata-info').appendChild(metadataBox);
                }
            }
            
            // Render module tree
            const moduleSection = document.createElement('div');
            moduleSection.className = 'nav-section';
            navTree.appendChild(moduleSection);
            renderNavTree(MODULE_TREE, moduleSection);
        }
        
        // Initialize router
        const router = new Router();
        
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

