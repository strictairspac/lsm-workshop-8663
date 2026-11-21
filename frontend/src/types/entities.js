/** @typedef {{id:number,name:string,description:string,entries:Array}} Dataset */
/** @typedef {{id:number,name:string,memtable_limit:number,l0_trigger:number,max_levels:number,final_compaction:boolean}} Config */
/** @typedef {{id:number,name:string,dataset_id:number,config_id:number,status:string,result:Object}} Trial */
/** @typedef {{id:number,name:string,value_threshold:number,value_capacity:number,occupancy:Object,gc:Object,verified:boolean}} ValueDrill */
export const entityNames={dataset:'键值批次',config:'存储配置',trial:'合并试验',valueDrill:'值空间演练'};
