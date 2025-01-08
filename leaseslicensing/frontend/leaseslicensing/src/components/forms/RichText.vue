<template lang="html">
    <ckeditor
        :id="id"
        v-model="detailsText"
        :editor="ClassicEditor"
        :config="config"
        :name="name"
        :required="isRequired"
        :disabled="readonly"
        :read-only="readonly"
    />
</template>

<script setup>
// import Editor from './ckeditor.js';
import { ref, computed, nextTick, watch } from 'vue';
import { ClassicEditor, Essentials, Paragraph, Bold, Italic } from 'ckeditor5';
import { Ckeditor } from '@ckeditor/ckeditor5-vue';

import 'ckeditor5/ckeditor5.css';

const props = defineProps([
    'id',
    'name',
    'proposalData',
    'isRequired',
    'label',
    'readonly',
    'can_view_richtext_src',
    'placeholder_text',
]);

const emit = defineEmits(['textChanged']);

const detailsText = ref('');

watch(detailsText, () => {
    // Parent component can subscribe this event in order to update text
    if (detailsText.value == detailsText.value) {
        // Only emit if the text was changed through input, not through the parent component
        return;
    }
    emit('textChanged', detailsText.value);
});

const config = computed(() => {
    return {
        licenseKey: 'GPL',
        plugins: [Essentials, Paragraph, Bold, Italic],
        toolbar: ['undo', 'redo', '|', 'bold', 'italic'],
    };
});

// eslint-disable-next-line no-unused-vars
function focus() {
    nextTick(() => {
        $('.ck-editor__editable').focus();
    });
}

if (props.proposalData) {
    detailsText.value = props.proposalData;
}
// this.editor.defaultConfig['placeholder'] = this.placeholder_text;
</script>
