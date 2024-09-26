async function loadManifest() {
    const response = await fetch('manifest.json');
    return await response.json();
}

async function loadModule(modulePath) {
    const module = await import(modulePath);
    return module.default;
}

async function initializeModules() {
    const manifest = await loadManifest();
    const container = document.getElementById('modules-container');

    for (const [index, modulePath] of manifest.modules.entries()) {
        const moduleContent = await loadModule(modulePath);
        const moduleElement = document.createElement('div');
        moduleElement.className = 'module';
        moduleElement.setAttribute('data-swapy-slot', `slot-${index}`);
        moduleElement.setAttribute('data-swapy-item', `item-${index}`);
        moduleElement.innerHTML = moduleContent;
        container.appendChild(moduleElement);
    }

    const swapy = Swapy.createSwapy(container, {
        animation: 'spring'
    });

    swapy.onSwap((event) => {
        console.log('Nouvel ordre des modules:', event.data.array);
    });
}

initializeModules();
