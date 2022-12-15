<template>
    <div>
        <CollapsibleFilters component_title="Filters" ref="collapsible_filters" @created="collapsible_component_mounted" class="mb-2">
                <!-- Filter by type -->
                <div class="row">
                    <div class="col-md-3">
                        <div class="form-group">
                            <label for="">Type</label>
                            <select class="form-control" v-model="filterComplianceType">
                                <option value="all">All</option>
                                <option v-for="_type in compliance_types" :value="_type.code">{{ type.description }}</option>
                            </select>
                        </div>
                    </div>
                    <!-- Filter by status -->
                    <div class="col-md-3">
                        <div class="form-group">
                            <label for="">Status</label>
                            <select class="form-control" v-model="filterComplianceStatus">
                                <option value="all">All</option>
                                <option v-for="status in compliance_statuses" :value="status.code">{{ status.description }}</option>
                            </select>
                        </div>
                    </div>
                    <!-- Filter by due data from -->
                    <div class="col-md-3">
                        <div class="form-group">
                            <label for="">Due Date From</label>
                            <div class="input-group date" ref="complianceDateFromPicker">
                                <input type="text" class="form-control" placeholder="DD/MM/YYYY" v-model="filterComplianceDueDateFrom">
                                <span class="input-group-addon">
                                    <span class="glyphicon glyphicon-calendar"></span>
                                </span>
                            </div>
                        </div>
                    </div>
                    <!-- Filter by due date to -->
                    <div class="col-md-3">
                        <div class="form-group">
                            <label for="">Due Date To</label>
                            <div class="input-group date" ref="complianceDateToPicker">
                                <input type="text" class="form-control" placeholder="DD/MM/YYYY" v-model="filterComplianceDueDateTo">
                                <span class="input-group-addon">
                                    <span class="glyphicon glyphicon-calendar"></span>
                                </span>
                            </div>
                        </div>
                    </div>
                </div>

        </CollapsibleFilters>

        <div class="row">
            <div class="col-lg-12">
                <datatable
                    ref="compliances_datatable"
                    :id="datatable_id"
                    :dtOptions="compliancesOptions"
                    :dtHeaders="compliancesHeaders"
                />
            </div>
        </div>
    </div>
</template>

<script>
import datatable from '@/utils/vue/datatable.vue'
// import Vue from 'vue'
import { api_endpoints, helpers }from '@/utils/hooks'
import CollapsibleFilters from '@/components/forms/collapsible_component.vue'
//import '@/components/common/filters.css'

export default {
    name: 'TableConditions',
    props: {
        level:{
            type: String,
            required: true,
            validator: function(val) {
                let options = ['internal', 'referral', 'external'];
                return options.indexOf(val) != -1 ? true: false;
            }
        },
        target_email_user_id: {
            type: Number,
            required: false,
            default: 0,
        },
    },
    data() {
        let vm = this;
        return {
            datatable_id: 'compliances-datatable-' + vm._uid,

            // selected values for filtering
            filterComplianceType: sessionStorage.getItem('filterComplianceType') ? sessionStorage.getItem('filterComplianceType') : 'all',
            filterComplianceStatus: sessionStorage.getItem('filterComplianceStatus') ? sessionStorage.getItem('filterComplianceStatus') : 'all',
            filterComplianceDueDateFrom: sessionStorage.getItem('filterComplianceDueDateFrom') ? sessionStorage.getItem('filterComplianceDueDateFrom') : '',
            filterComplianceDueDateTo: sessionStorage.getItem('filterComplianceDueDateTo') ? sessionStorage.getItem('filterComplianceDueDateTo') : '',

            // filtering options
            compliance_types: [],
            compliance_statuses: [],

            // Filters toggle
            filters_expanded: false,

            dateFormat: 'DD/MM/YYYY',
            datepickerOptions:{
                format: 'DD/MM/YYYY',
                showClear:true,
                useCurrent:false,
                keepInvalid:true,
                allowInputToggle:true
            },

        }
    },
    components:{
        datatable,
        CollapsibleFilters,
    },
    watch: {
        filterComplianceStatus: function() {
            this.$refs.compliances_datatable.vmDataTable.draw();  // This calls ajax() backend call.  This line is enough to search?  Do we need following lines...?
            sessionStorage.setItem('filterComplianceStatus', this.filterComplianceStatus);
        },
        filterComplianceType: function() {
            this.$refs.compliances_datatable.vmDataTable.draw();  // This calls ajax() backend call.  This line is enough to search?  Do we need following lines...?
            sessionStorage.setItem('filterComplianceType', this.filterComplianceType);
        },
        filterComplianceDueDateFrom: function() {
            this.$refs.compliances_datatable.vmDataTable.draw();  // This calls ajax() backend call.  This line is enough to search?  Do we need following lines...?
            sessionStorage.setItem('filterComplianceDueDateFrom', this.filterComplianceDueDateFrom);
        },
        filterComplianceDueDateTo: function() {
            this.$refs.compliances_datatable.vmDataTable.draw();  // This calls ajax() backend call.  This line is enough to search?  Do we need following lines...?
            sessionStorage.setItem('filterComplianceDueDateTo', this.filterComplianceDueDateTo);
        },
        filterApplied: function(){
            if (this.$refs.collapsible_filters){
                // Collapsible component exists
                this.$refs.collapsible_filters.show_warning_icon(this.filterApplied)
            }
        }
    },
    computed: {
        filterApplied: function(){
            if(this.filterComplianceStatus.toLowerCase() === 'all' && this.filterComplianceType.toLowerCase() === 'all' &&
                this.filterComplianceDueDateFrom.toLowerCase() === '' && this.filterComplianceDueDateTo.toLowerCase() === ''){
                return false
            } else {
                return true
            }
        },
        is_external: function() {
            return this.level == 'external'
        },
        is_internal: function() {
            return this.level == 'internal'
        },
        compliancesHeaders: function() {
            // Table header
            // let headers = ['Number', 'Licence/Permit', 'Condition', 'Due Date', 'Status', 'Action'];
            let headers = ['Number', 'Type', 'Holder', 'Approval', 'Status', 'Due Date', 'Action'];
            if (this.level === 'internal') {
                headers = ['Number', 'Type',  'Holder', 'Approval Number','Status', 'Due Date', 'Action'];
            }
            return headers;
        },
        lodgementNumberColumn: function() {
            return {
                        // 1. Lodgement Number
                        data: "id",
                        orderable: true,
                        searchable: true,
                        visible: true,
                        'render': function(row, type, full){
                            return full.lodgement_number;
                        }
                    }
        },
        applicationTypeColumn: function() {
            return {
                        // 2. Type
                        data: "id",
                        orderable: true,
                        searchable: true,
                        visible: true,
                        'render': function(row, type, full){
                            return full.application_type;
                            //return full.id;
                        }
                    }
        },
        holderColumn: function() {
            return {
                        // 3. Holder
                        data: "id",
                        orderable: true,
                        searchable: true,
                        visible: true,
                        'render': function(row, type, full){
                            //return full.approval_submitter;
                            return full.holder;
                        }
                    }
        },
        // Approval
        approvalColumn: function() {
            return {
                        // 4. Approval
                        data: "id",
                        orderable: true,
                        searchable: true,
                        visible: true,
                        'render': function(row, type, full){
                            return full.approval_lodgement_number
                            //return full.id;
                        }
                    }
        },

        licenceNumberColumn: function() {
            return {
                        // 3. Licence/Permit
                        data: "id",
                        orderable: true,
                        searchable: true,
                        visible: true,
                        'render': function(row, type, full){
                            return full.approval_lodgement_number
                            //return full.id;
                        }
                    }
        },
        conditionColumn: function() {
            return {
                        // 4. Condition
                        data: "id",
                        orderable: true,
                        searchable: true,
                        visible: true,
                        'render': function(row, type, full){
                            let requirement = '';
                            if (full.requirement) {
                                requirement = full.requirement.requirement;
                            }
                            //return requirement;
                            return full.id;
                        }
                    }
        },
        statusColumn: function() {
            return {
                        // 5. Status
                        data: "id",
                        orderable: true,
                        searchable: true,
                        visible: true,
                        'render': function(row, type, full){
                            return full.processing_status
                            //return full.id;
                        }
                    }
        },
        dueDateColumn: function() {
            return {
                        // 6. Due Date
                        data: "id",
                        orderable: true,
                        searchable: true,
                        visible: true,
                        'render': function(row, type, full){
                            let dueDate = '';
                            if (full.requirement) {
                                dueDate = full.requirement.read_due_date;
                            }
                            //return dueDate;
                            return full.due_date;
                        }
                    }
        },
        actionColumn: function() {
            let vm = this;
            return {
                        // 7. Action
                        data: "id",
                        orderable: true,
                        searchable: true,
                        visible: true,
                        'render': function(row, type, full){
                            let links = '';
                            if (!vm.is_external){
                                //if (full.processing_status=='With Assessor' && vm.check_assessor(full)) {
                                if (full.can_process) {
                                    links +=  `<a href='/internal/compliance/${full.id}'>Process</a><br/>`;

                                }
                                else {
                                    links +=  `<a href='/internal/compliance/${full.id}'>View</a><br/>`;
                                }
                            }
                            else{
                                if (full.can_user_view) {
                                    links +=  `<a href='/external/compliance/${full.id}'>View</a><br/>`;

                                }
                                else {
                                    links +=  `<a href='/external/compliance/${full.id}'>Submit</a><br/>`;
                                }
                            }
                            return links;
                        }
                    }
        },
        assignedToNameColumn: function() {
            return {
                        // 7. Action
                        data: "id",
                        orderable: true,
                        searchable: true,
                        visible: true,
                        'render': function(row, type, full){
                            //return full.assigned_to_name;
                            return full.id;
                        }
                    }
        },

        applicableColumns: function() {
            let columns = [
                this.lodgementNumberColumn,
                this.licenceNumberColumn,
                this.holderColumn,
                this.conditionColumn,
                this.statusColumn,
                this.dueDateColumn,
                this.actionColumn,
                ]
            if (this.level === 'internal') {
                columns = [
                    this.lodgementNumberColumn,
                    this.applicationTypeColumn,
                    this.holderColumn,
                    this.licenceNumberColumn,
                    //this.conditionColumn,
                    this.statusColumn,
                    this.dueDateColumn,
                    //this.assignedToNameColumn,
                    this.actionColumn,
                    ]
            }
            return columns;
        },
        compliancesOptions: function() {
            let vm = this;
            let buttons = []
            if (this.level === 'internal'){
                buttons = [
                    {
                        extend: 'excel',
                        text: '<i class="fa-solid fa-download"></i> Excel',
                        className: 'btn btn-primary ml-2',
                        exportOptions: {
                            columns: ':visible'
                        }
                    },
                    {
                        extend: 'csv',
                        text: '<i class="fa-solid fa-download"></i> CSV',
                        className: 'btn btn-primary',
                        exportOptions: {
                            columns: ':visible'
                        }
                    },
                ]
            }

            return {
                searching: false,
                autoWidth: false,
                language: {
                    processing: "<i class='fa fa-4x fa-spinner fa-spin'></i>"
                },
                responsive: true,
                serverSide: true,
                searching: true,

                ajax: {
                    "url": api_endpoints.compliances_paginated_external + '?format=datatables&target_email_user_id=' + vm.target_email_user_id,
                    "dataSrc": 'data',

                    // adding extra GET params for Custom filtering
                    "data": function ( d ) {
                        // Add filters selected
                        d.filter_compliance_status = vm.filterComplianceStatus;
                        d.filter_lodged_from = vm.filterProposalLodgedFrom;
                        d.filter_lodged_to = vm.filterProposalLodgedTo;
                    }
                },
                //dom: 'lBfrtip',
                dom: "<'d-flex align-items-center'<'me-auto'l>fB>" +
                     "<'row'<'col-sm-12'tr>>" +
                     "<'d-flex align-items-center'<'me-auto'i>p>",
                buttons: buttons,
                /*

                buttons:[
                    //{
                    //    extend: 'csv',
                    //    exportOptions: {
                    //        columns: ':visible'
                    //    }
                    //},
                ],
                */
                columns: vm.applicableColumns,
                processing: true,
                initComplete: function(r) {
                    console.log('in initComplete', r)
                },
            }
        },

    },
    methods: {
        collapsible_component_mounted: function(){
            this.$refs.collapsible_filters.show_warning_icon(this.filterApplied)
        },
        expandCollapseFilters: function(){
            this.filters_expanded = !this.filters_expanded
        },
        fetchFilterLists: function(){
            let vm = this;

            // Statuses
            fetch(api_endpoints.compliance_statuses_dict).then(async(response) => {
                vm.compliance_statuses = await response.json();
            },(error) => {
                console.log(error);
            })
        },
        addEventListeners: function(){
            let vm = this;
            /*
            // update to bs5
            // Lodged From
            $(vm.$refs.complianceDateFromPicker).datetimepicker(vm.datepickerOptions);
            $(vm.$refs.complianceDateFromPicker).on('dp.change',function (e) {
                if ($(vm.$refs.complianceDateFromPicker).data('DateTimePicker').date()) {
                    // DateFrom has been picked
                    vm.filterComplianceDueDateFrom = e.date.format('DD/MM/YYYY');
                    $(vm.$refs.complianceDateToPicker).data("DateTimePicker").minDate(e.date);
                }
                else if ($(vm.$refs.complianceDateFromPicker).data('date') === "") {
                    vm.filterComplianceDueDateFrom = "";
                    $(vm.$refs.complianceDateToPicker).data("DateTimePicker").minDate(false);
                }
            });

            // Lodged To
            $(vm.$refs.complianceDateToPicker).datetimepicker(vm.datepickerOptions);
            $(vm.$refs.complianceDateToPicker).on('dp.change',function (e) {
                if ($(vm.$refs.complianceDateToPicker).data('DateTimePicker').date()) {
                    // DateTo has been picked
                    vm.filterComplianceDueDateTo = e.date.format('DD/MM/YYYY');
                    $(vm.$refs.complianceDateFromPicker).data("DateTimePicker").maxDate(e.date);
                }
                else if ($(vm.$refs.complianceDateToPicker).data('date') === "") {
                    vm.filterComplianceDueDateTo = "";
                    $(vm.$refs.complianceDateFromPicker).data("DateTimePicker").maxDate(false);
                }
            });
            */
        }
    },
    created: function(){
        this.fetchFilterLists()
    },
    mounted: function(){
        this.$nextTick(() => {
            this.addEventListeners();
        });
    }
}
</script>

<style scoped>
</style>
