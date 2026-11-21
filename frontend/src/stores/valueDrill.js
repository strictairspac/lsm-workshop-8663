import{list}from'../api/valueDrill.js';export const valueDrills={items:[],async load(){this.items=await list();return this.items}};
